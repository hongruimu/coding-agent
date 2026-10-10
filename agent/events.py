import json
import re
import shlex
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


MAX_EVENT_TEXT_CHARS = 500
MAX_QUERY_CHARS = 200
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "content",
        "new_text",
        "old_text",
        "password",
        "secret",
        "token",
    }
)
SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)\b([\w-]*(?:api[_-]?key|authorization|password|secret|token)[\w-]*)\s*[:=]\s*([^\s,;]+)"
)
OPENAI_KEY_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")
SHELL_ASSIGNMENT_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class RunEvent:
    schema_version: int
    type: str
    run_id: str
    sequence: int
    timestamp: str
    elapsed_ms: int
    step: int | None
    phase: str | None
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class EventLogger(Protocol):
    def emit(self, event: RunEvent) -> None:
        pass


class NullEventLogger:
    def emit(self, event: RunEvent) -> None:
        return None


class JsonlEventLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def emit(self, event: RunEvent) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event.as_dict(), ensure_ascii=False) + "\n")


class RunTrace:
    def __init__(self, logger: EventLogger | None = None, *, run_id: str | None = None):
        self.logger = logger or NullEventLogger()
        self.run_id = run_id or uuid.uuid4().hex
        self._started_at = time.monotonic()
        self._sequence = 0

    def emit(
        self,
        event_type: str,
        *,
        step: int | None = None,
        phase: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> RunEvent:
        self._sequence += 1
        event = RunEvent(
            schema_version=1,
            type=event_type,
            run_id=self.run_id,
            sequence=self._sequence,
            timestamp=datetime.now(timezone.utc).isoformat(),
            elapsed_ms=max(int((time.monotonic() - self._started_at) * 1000), 0),
            step=step,
            phase=phase,
            data=_sanitize(data or {}),
        )
        self.logger.emit(event)
        return event


def summarize_tool_arguments(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    path = arguments.get("path")
    if isinstance(path, str):
        summary["path"] = _truncate(path, MAX_QUERY_CHARS)

    for key in ("pattern", "query"):
        value = arguments.get(key)
        if isinstance(value, str):
            summary[key] = _truncate(value, MAX_QUERY_CHARS)

    timeout = arguments.get("timeout")
    if isinstance(timeout, int):
        summary["timeout"] = timeout

    if tool_name == "run_command" and isinstance(arguments.get("command"), str):
        command = arguments["command"]
        summary["command_name"] = _command_name(command)
        summary["command_chars"] = len(command)

    for key in ("content", "old_text", "new_text"):
        value = arguments.get(key)
        if isinstance(value, str):
            summary[f"{key}_chars"] = len(value)

    return summary


def _sanitize(value: Any, key: str | None = None) -> Any:
    if key is not None and _is_sensitive_key(key):
        if isinstance(value, str):
            return f"[redacted {len(value)} chars]"
        return "[redacted]"
    if isinstance(value, dict):
        return {str(item_key): _sanitize(item_value, str(item_key)) for item_key, item_value in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return _truncate(_redact_string(value), MAX_EVENT_TEXT_CHARS)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _truncate(str(value), MAX_EVENT_TEXT_CHARS)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold()
    return normalized in SENSITIVE_KEYS or any(
        part in normalized for part in ("api_key", "authorization", "password", "secret", "token")
    )


def _command_name(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    for part in parts:
        if SHELL_ASSIGNMENT_PATTERN.match(part):
            continue
        return _truncate(part, MAX_QUERY_CHARS)
    return ""


def _redact_string(value: str) -> str:
    redacted = SENSITIVE_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}=[redacted]", value)
    return OPENAI_KEY_PATTERN.sub("[redacted-api-key]", redacted)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"... truncated after {max_chars} characters"
