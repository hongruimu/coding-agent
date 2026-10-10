import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from agent.context_plan import ContextPlan
from agent.task_spec import TaskSpec
from tools.registry import ToolRegistry


SYSTEM_PROMPT = """You are coding-agent, a practical Python coding assistant.

Your goal is not to imitate Codex internals. Your goal is to help the user finish real coding work through this loop:
1. Understand the request and inspect the workspace before changing files.
2. Make a short plan for non-trivial tasks.
3. Execute focused changes with available tools.
4. Validate with the most relevant command when possible.
5. Report what changed, validation results, and any remaining risk.

Rules:
- Prefer small, reversible steps.
- Use repo_map to understand project structure, then search_files or grep_code to locate relevant files before reading them.
- Read files before editing them.
- Use write_file only to create new files. Use replace_text for focused edits to existing files.
- Review the git diff returned after each successful edit before continuing.
- Keep changes minimal and directly related to the user request.
- Avoid destructive commands such as rm, git reset, and force pushes unless the user explicitly asks.
- Do not invent command results; run commands when validation matters.
"""


DISCOVERY_TOOLS = frozenset({"repo_map", "search_files", "grep_code", "list_files", "read_file"})
EVALUATION_TOOLS = frozenset({"read_file", "git_diff"})


class AgentPhase(str, Enum):
    UNDERSTAND = "understand"
    PLAN = "plan"
    EXECUTE = "execute"
    EVALUATE = "evaluate"
    FINALIZE = "finalize"


@dataclass
class AgentResult:
    answer: str
    steps_used: int
    changed_files: tuple[str, ...] = ()
    phase: AgentPhase = AgentPhase.FINALIZE


class ChatClient(Protocol):
    def create(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        pass


class CodingAgent:
    def __init__(
        self,
        client: ChatClient,
        registry: ToolRegistry,
        model: str,
        workspace: str | Path,
        task_spec: TaskSpec,
        context_plan: ContextPlan,
        max_steps: int = 12,
    ):
        self.client = client
        self.registry = registry
        self.model = model
        self.workspace = Path(workspace).expanduser().resolve()
        self.task_spec = task_spec
        self.context_plan = context_plan
        self.max_steps = max_steps
        self._read_files: set[str] = set()
        self._changed_files: list[str] = []

    def run(self, user_prompt: str) -> AgentResult:
        self._read_files.clear()
        self._changed_files.clear()
        phase = AgentPhase.UNDERSTAND
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": (
                    f"Current workspace: {self.workspace}\n"
                    "All tool paths are resolved relative to this workspace. "
                    "Do not assume paths in the user's prompt are active workspaces unless the CLI selected them."
                ),
            },
            {"role": "system", "content": self.task_spec.format_for_prompt()},
            {
                "role": "system",
                "content": (
                    f"{self.context_plan.format_for_prompt()}\n"
                    "Read must_read files before making changes. Read maybe_read files only when needed. "
                    "Do not read forbidden paths, and stay within the context budget."
                ),
            },
            {"role": "user", "content": user_prompt},
            self._phase_message(phase),
        ]

        latest_content = ""
        for step in range(1, self.max_steps + 1):
            allowed_tools = self._allowed_tools(phase)
            response = self.client.create(
                model=self.model,
                messages=messages,
                tools=self.registry.definitions(allowed_tools),
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            messages.append(self._assistant_message(message))

            if message.content:
                latest_content = message.content

            if tool_calls:
                for tool_call in tool_calls:
                    result = self._execute_tool(tool_call, allowed_tools=allowed_tools)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": result,
                        }
                    )
                continue

            if phase is AgentPhase.FINALIZE:
                return AgentResult(
                    answer=latest_content,
                    steps_used=step,
                    changed_files=tuple(self._changed_files),
                    phase=phase,
                )

            phase = self._next_phase(phase, message.content or "")
            messages.append(self._phase_message(phase))

        changed = ", ".join(self._changed_files) if self._changed_files else "none"
        limit_message = (
            f"Step limit reached during {phase.value} phase. "
            f"Changed files: {changed}. The task may be incomplete."
        )
        answer = f"{latest_content}\n\n{limit_message}" if latest_content else limit_message
        return AgentResult(
            answer=answer,
            steps_used=self.max_steps,
            changed_files=tuple(self._changed_files),
            phase=phase,
        )

    def _phase_message(self, phase: AgentPhase) -> dict[str, str]:
        instructions = {
            AgentPhase.UNDERSTAND: (
                "Inspect the repository evidence needed to understand the request. "
                "Do not modify files or run shell commands. When the request is understood, respond with a concise understanding."
            ),
            AgentPhase.PLAN: (
                "Create a short, concrete execution plan based on inspected evidence. "
                "Do not modify files or run shell commands."
            ),
            AgentPhase.EXECUTE: (
                "Carry out the plan using focused changes. Read before editing, use replace_text for existing files, "
                "and inspect each automatically returned diff."
            ),
            AgentPhase.EVALUATE: (
                "Review changed files and diffs, then run the most relevant available validation. "
                "Do not modify files in this phase. If a fix is still required, start the phase summary with "
                "[NEEDS_CHANGES] so the program can return to Execute."
            ),
            AgentPhase.FINALIZE: (
                "Produce the final answer without calling tools. Summarize the result, changed files, validation, "
                "and remaining risks."
            ),
        }
        changed = ", ".join(self._changed_files) if self._changed_files else "none"
        return {
            "role": "system",
            "content": f"Current phase: {phase.value}\nChanged files so far: {changed}\n{instructions[phase]}",
        }

    def _allowed_tools(self, phase: AgentPhase) -> set[str]:
        available = set(self.registry.names())
        if phase in {AgentPhase.UNDERSTAND, AgentPhase.PLAN}:
            return available.intersection(DISCOVERY_TOOLS)
        if phase is AgentPhase.EXECUTE:
            return available.intersection(self.task_spec.allowed_tools())
        if phase is AgentPhase.EVALUATE:
            allowed = set(EVALUATION_TOOLS)
            if self.task_spec.shell_allowed:
                allowed.add("run_command")
            return available.intersection(allowed)
        return set()

    def _next_phase(self, phase: AgentPhase, phase_output: str = "") -> AgentPhase:
        if phase is AgentPhase.UNDERSTAND:
            if self.task_spec.plan_required:
                return AgentPhase.PLAN
            if self.task_spec.write_allowed:
                return AgentPhase.EXECUTE
            return AgentPhase.FINALIZE
        if phase is AgentPhase.PLAN:
            return AgentPhase.EXECUTE if self.task_spec.write_allowed else AgentPhase.FINALIZE
        if phase is AgentPhase.EXECUTE:
            return AgentPhase.EVALUATE
        if phase is AgentPhase.EVALUATE:
            if phase_output.lstrip().casefold().startswith("[needs_changes]"):
                return AgentPhase.EXECUTE
            return AgentPhase.FINALIZE
        return AgentPhase.FINALIZE

    def _assistant_message(self, message: Any) -> dict[str, Any]:
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }

        if message.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in message.tool_calls
            ]

        return assistant_message

    def _execute_tool(self, tool_call: Any, allowed_tools: set[str] | None = None) -> str:
        name = tool_call.function.name
        if allowed_tools is not None and name not in allowed_tools:
            return f"Tool {name} blocked: it is not allowed during the current phase."

        try:
            arguments = json.loads(tool_call.function.arguments or "{}")
            read_path = self._read_path_for_budget(name, arguments)
            if read_path is not None and self._is_forbidden_read(read_path):
                return f"Tool {name} blocked: path is forbidden by the context plan: {read_path}"
            if read_path is not None and read_path not in self._read_files:
                if len(self._read_files) >= self.context_plan.max_files:
                    return (
                        f"Tool {name} blocked: context file budget of "
                        f"{self.context_plan.max_files} distinct files has been reached."
                    )
            result = self.registry.execute(name, arguments)
            if read_path is not None:
                self._read_files.add(read_path)
            changed_path = self._changed_path(name, arguments)
            if changed_path is not None:
                if changed_path not in self._changed_files:
                    self._changed_files.append(changed_path)
                result = self._append_diff(result, changed_path)
        except Exception as exc:
            return f"Tool {name} failed: {type(exc).__name__}: {exc}"

        if isinstance(result, str):
            return result

        return json.dumps(result, ensure_ascii=False)

    def _changed_path(self, name: str, arguments: dict[str, Any]) -> str | None:
        if name not in {"write_file", "replace_text"} or not isinstance(arguments.get("path"), str):
            return None

        requested = Path(arguments["path"]).expanduser()
        absolute = requested.resolve() if requested.is_absolute() else (self.workspace / requested).resolve()
        try:
            return absolute.relative_to(self.workspace).as_posix()
        except ValueError:
            return None

    def _append_diff(self, result: Any, path: str) -> str:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
        if "git_diff" not in self.registry.names():
            return text

        try:
            diff = self.registry.execute("git_diff", {"path": path})
        except Exception as exc:
            diff = f"git_diff failed: {type(exc).__name__}: {exc}"
        return f"{text}\n\n{diff}"

    def _read_path_for_budget(self, name: str, arguments: dict[str, Any]) -> str | None:
        if name != "read_file" or not isinstance(arguments.get("path"), str):
            return None

        requested = Path(arguments["path"]).expanduser()
        absolute = requested.resolve() if requested.is_absolute() else (self.workspace / requested).resolve()
        try:
            return absolute.relative_to(self.workspace).as_posix()
        except ValueError:
            return str(absolute)

    def _is_forbidden_read(self, path: str) -> bool:
        path_parts = Path(path).parts
        for forbidden in self.context_plan.forbidden:
            normalized = forbidden.rstrip("/")
            if forbidden.endswith("/") and normalized in path_parts:
                return True
            if not forbidden.endswith("/") and Path(path).name == normalized:
                return True
        return False
