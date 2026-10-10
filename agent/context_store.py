import json
from dataclasses import dataclass
from typing import Any

from agent.validation import ValidationResult


DEFAULT_PHASE_TOOL_BUDGET = 48_000
DEFAULT_RESULT_BUDGET = 16_000
DEFAULT_PROMPT_BUDGET = 24_000
MAX_PHASE_SUMMARY_CHARS = 3_000
MAX_ARTIFACTS_IN_PROMPT = 10
MAX_EVIDENCE_IN_PROMPT = 60


@dataclass(frozen=True)
class ToolObservation:
    phase: str
    step: int
    tool_name: str
    arguments: dict[str, Any]
    raw_result: str
    visible_result: str
    success: bool


@dataclass(frozen=True)
class EvidenceItem:
    phase: str
    kind: str
    source: str
    detail: str
    status: str = "observed"


@dataclass(frozen=True)
class PhaseArtifact:
    phase: str
    summary: str
    tool_calls: int
    files_read: tuple[str, ...]
    searches: tuple[str, ...]
    changed_files: tuple[str, ...]
    validation_status: str


class ContextStore:
    def __init__(
        self,
        max_phase_tool_chars: int = DEFAULT_PHASE_TOOL_BUDGET,
        max_result_chars: int = DEFAULT_RESULT_BUDGET,
        max_prompt_chars: int = DEFAULT_PROMPT_BUDGET,
    ):
        if max_phase_tool_chars < 1 or max_result_chars < 1 or max_prompt_chars < 1_000:
            raise ValueError("Tool budgets must be positive and prompt budget must be at least 1000 characters")
        self.max_phase_tool_chars = max_phase_tool_chars
        self.max_result_chars = max_result_chars
        self.max_prompt_chars = max_prompt_chars
        self.observations: list[ToolObservation] = []
        self.evidence: list[EvidenceItem] = []
        self.phase_artifacts: list[PhaseArtifact] = []
        self._phase_start_observation = 0
        self._phase_visible_chars = 0

    def reset(self) -> None:
        self.observations.clear()
        self.evidence.clear()
        self.phase_artifacts.clear()
        self._phase_start_observation = 0
        self._phase_visible_chars = 0

    def record_tool_result(
        self,
        *,
        phase: str,
        step: int,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
    ) -> str:
        visible = self._visible_result(result, tool_name)
        success = not result.startswith(f"Tool {tool_name} failed:") and not result.startswith(
            f"Tool {tool_name} blocked:"
        )
        self.observations.append(
            ToolObservation(
                phase=phase,
                step=step,
                tool_name=tool_name,
                arguments=dict(arguments),
                raw_result=result,
                visible_result=visible,
                success=success,
            )
        )
        self._record_tool_evidence(phase, tool_name, arguments, result, success)
        return visible

    def record_validation(self, phase: str, results: tuple[ValidationResult, ...]) -> None:
        for result in results:
            detail = f"exit_code={result.exit_code}; command={' '.join(result.argv)}"
            if not result.passed:
                output = result.stderr.strip() or result.stdout.strip()
                if output:
                    detail += f"; output={_truncate(output, 400)}"
            self._add_evidence(
                EvidenceItem(
                    phase=phase,
                    kind="validation",
                    source=result.name,
                    detail=detail,
                    status="passed" if result.passed else "failed",
                )
            )

    def complete_phase(
        self,
        *,
        phase: str,
        summary: str,
        changed_files: tuple[str, ...],
        validation_results: tuple[ValidationResult, ...] = (),
    ) -> PhaseArtifact:
        observations = self.observations[self._phase_start_observation :]
        files_read = _unique(
            str(item.arguments.get("path"))
            for item in observations
            if item.tool_name == "read_file" and isinstance(item.arguments.get("path"), str)
        )
        searches = _unique(
            _search_description(item.tool_name, item.arguments)
            for item in observations
            if item.tool_name in {"search_files", "grep_code"}
        )
        if not validation_results:
            validation_status = "not run"
        else:
            validation_status = "passed" if all(result.passed for result in validation_results) else "failed"
        artifact = PhaseArtifact(
            phase=phase,
            summary=_truncate(summary.strip() or "Phase completed without a textual summary.", MAX_PHASE_SUMMARY_CHARS),
            tool_calls=len(observations),
            files_read=files_read,
            searches=searches,
            changed_files=tuple(dict.fromkeys(changed_files)),
            validation_status=validation_status,
        )
        self.phase_artifacts.append(artifact)
        self._phase_start_observation = len(self.observations)
        self._phase_visible_chars = 0
        return artifact

    def format_for_prompt(self) -> str:
        artifacts = [
            {
                "phase": item.phase,
                "summary": item.summary,
                "tool_calls": item.tool_calls,
                "files_read": list(item.files_read),
                "searches": list(item.searches),
                "changed_files": list(item.changed_files),
                "validation_status": item.validation_status,
            }
            for item in self.phase_artifacts[-MAX_ARTIFACTS_IN_PROMPT:]
        ]
        evidence = [
            {
                "phase": item.phase,
                "kind": item.kind,
                "source": item.source,
                "detail": _truncate(item.detail, 800),
                "status": item.status,
            }
            for item in self.evidence[-MAX_EVIDENCE_IN_PROMPT:]
        ]
        text = self._render_prompt(artifacts, evidence)
        while len(text) > self.max_prompt_chars and len(evidence) > 5:
            evidence.pop(0)
            text = self._render_prompt(artifacts, evidence)
        while len(text) > self.max_prompt_chars and len(artifacts) > 1:
            artifacts.pop(0)
            text = self._render_prompt(artifacts, evidence)
        if len(text) > self.max_prompt_chars:
            for artifact in artifacts:
                artifact["summary"] = _truncate(artifact["summary"], 300)
            for item in evidence:
                item["detail"] = _truncate(item["detail"], 100)
            text = self._render_prompt(artifacts, evidence)
        while len(text) > self.max_prompt_chars and evidence:
            evidence.pop(0)
            text = self._render_prompt(artifacts, evidence)
        if len(text) > self.max_prompt_chars:
            latest = artifacts[-1] if artifacts else None
            payload = {
                "latest_phase_artifact": latest,
                "phase_artifact_count": len(self.phase_artifacts),
                "evidence_count": len(self.evidence),
                "raw_tool_results_retained": len(self.observations),
            }
            text = "Context store:\n" + json.dumps(payload, ensure_ascii=False, indent=2)
        return text

    def _render_prompt(self, artifacts: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> str:
        payload = {
            "phase_artifacts": artifacts,
            "evidence_table": evidence,
            "raw_tool_results_retained": len(self.observations),
        }
        return "Context store:\n" + json.dumps(payload, ensure_ascii=False, indent=2)

    def _visible_result(self, result: str, tool_name: str) -> str:
        critical = tool_name in {"write_file", "replace_text", "git_diff", "run_command"}
        reserved = 0 if critical else self.max_result_chars
        remaining = max(self.max_phase_tool_chars - self._phase_visible_chars - reserved, 0)
        allowed = min(self.max_result_chars, remaining)
        if allowed == 0:
            return "Tool result stored in ContextStore but omitted from active context because the phase budget was reached."
        visible = _truncate(result, allowed)
        self._phase_visible_chars += len(visible)
        return visible

    def _record_tool_evidence(
        self,
        phase: str,
        tool_name: str,
        arguments: dict[str, Any],
        result: str,
        success: bool,
    ) -> None:
        status = "success" if success else "failed"
        failure_detail = f" Failure: {_truncate(result, 300)}" if not success else ""
        path = arguments.get("path")
        if tool_name == "read_file" and isinstance(path, str):
            self._add_evidence(EvidenceItem(phase, "file", path, f"File content inspected.{failure_detail}", status))
        elif tool_name in {"search_files", "grep_code"}:
            self._add_evidence(
                EvidenceItem(
                    phase,
                    "search",
                    str(path or "."),
                    _search_description(tool_name, arguments) + failure_detail,
                    status,
                )
            )
        elif tool_name in {"write_file", "replace_text"} and isinstance(path, str):
            self._add_evidence(EvidenceItem(phase, "change", path, f"Changed with {tool_name}.{failure_detail}", status))
            if success:
                self._add_evidence(
                    EvidenceItem(phase, "diff", path, "Automatic diff returned after edit.", "success")
                )
        elif tool_name == "git_diff":
            self._add_evidence(
                EvidenceItem(phase, "diff", str(path or "."), f"Git diff inspected.{failure_detail}", status)
            )
        elif tool_name == "repo_map":
            self._add_evidence(
                EvidenceItem(phase, "repository", str(path or "."), f"Repository map inspected.{failure_detail}", status)
            )

    def _add_evidence(self, item: EvidenceItem) -> None:
        key = (item.phase, item.kind, item.source, item.detail, item.status)
        if any((current.phase, current.kind, current.source, current.detail, current.status) == key for current in self.evidence):
            return
        self.evidence.append(item)


def _search_description(tool_name: str, arguments: dict[str, Any]) -> str:
    if tool_name == "grep_code":
        return f"grep_code query={arguments.get('query', '')}"
    return f"search_files pattern={arguments.get('pattern', '')}"


def _unique(values) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for value in values if value))


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    suffix = f"\n... truncated after {max_chars} characters"
    if max_chars <= len(suffix):
        return suffix[:max_chars]
    return text[: max_chars - len(suffix)] + suffix
