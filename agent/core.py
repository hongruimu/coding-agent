import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Protocol

from agent.completion import CompletionReport, evaluate_completion
from agent.context_plan import ContextPlan
from agent.context_store import ContextStore, EvidenceItem, PhaseArtifact
from agent.execution_plan import (
    ExecutionPlan,
    WorkItemState,
    WorkItemTracker,
    parse_execution_plan,
)
from agent.project_instructions import ProjectInstructions, build_project_instructions
from agent.task_spec import TaskSpec
from agent.validation import ValidationPlan, ValidationResult, build_validation_plan, execute_validation_plan
from tools.repo import RepoMap, build_repo_map
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
    validation_results: tuple[ValidationResult, ...] = ()
    phase_artifacts: tuple[PhaseArtifact, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    execution_plan: ExecutionPlan | None = None
    work_items: tuple[WorkItemState, ...] = ()
    completion_report: CompletionReport | None = None
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
        repo_map: RepoMap | None = None,
        context_store: ContextStore | None = None,
        max_completion_retries: int = 2,
        max_plan_retries: int = 2,
        project_instructions: ProjectInstructions | None = None,
    ):
        self.client = client
        self.registry = registry
        self.model = model
        self.workspace = Path(workspace).expanduser().resolve()
        self.task_spec = task_spec
        self.context_plan = context_plan
        self.repo_map = repo_map or build_repo_map(workspace=self.workspace)
        self.project_instructions = project_instructions or build_project_instructions(
            self.workspace, self.repo_map.files
        )
        self.context_store = context_store or ContextStore()
        self.max_steps = max_steps
        self.max_completion_retries = max_completion_retries
        self.max_plan_retries = max_plan_retries
        self._read_files: set[str] = set()
        self._changed_files: list[str] = []
        self._validation_results: list[ValidationResult] = []
        self._validation_plan: ValidationPlan | None = None
        self._completion_report: CompletionReport | None = None
        self._completion_attempt = 0
        self._execution_plan: ExecutionPlan | None = None
        self._work_item_tracker: WorkItemTracker | None = None
        self._plan_attempt = 0

    def run(self, user_prompt: str) -> AgentResult:
        self._read_files.clear()
        self._changed_files.clear()
        self._validation_results.clear()
        self._validation_plan = None
        self._completion_report = None
        self._completion_attempt = 0
        self._execution_plan = None
        self._work_item_tracker = None
        self._plan_attempt = 0
        self.context_store.reset()
        phase = AgentPhase.UNDERSTAND
        base_messages: list[dict[str, Any]] = [
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
        ]
        messages = [*base_messages]
        instruction_message = self._project_instruction_message(phase)
        if instruction_message is not None:
            messages.append(instruction_message)
        messages.append(self._phase_message(phase))

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
                    arguments = self._tool_arguments(tool_call)
                    result = self._execute_tool(tool_call, allowed_tools=allowed_tools)
                    visible_result = self.context_store.record_tool_result(
                        phase=phase.value,
                        step=step,
                        tool_name=tool_call.function.name,
                        arguments=arguments,
                        result=result,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tool_call.id,
                            "content": visible_result,
                        }
                    )
                continue

            if phase is AgentPhase.FINALIZE:
                self.context_store.complete_phase(
                    phase=phase.value,
                    summary=latest_content,
                    changed_files=tuple(self._changed_files),
                    validation_results=tuple(self._validation_results),
                )
                return AgentResult(
                    answer=latest_content,
                    steps_used=step,
                    changed_files=tuple(self._changed_files),
                    validation_results=tuple(self._validation_results),
                    phase_artifacts=tuple(self.context_store.phase_artifacts),
                    evidence=tuple(self.context_store.evidence),
                    execution_plan=self._execution_plan,
                    work_items=self._work_items(),
                    completion_report=self._completion_report,
                    phase=phase,
                )

            self.context_store.complete_phase(
                phase=phase.value,
                summary=message.content or "",
                changed_files=tuple(self._changed_files),
                validation_results=tuple(self._validation_results) if phase is AgentPhase.EVALUATE else (),
            )
            phase_feedback = None
            if phase is AgentPhase.PLAN:
                self._plan_attempt += 1
                plan_result = parse_execution_plan(message.content or "", self.task_spec)
                phase_feedback = {"role": "system", "content": plan_result.format_for_prompt()}
                if plan_result.ready:
                    assert plan_result.plan is not None
                    self._execution_plan = plan_result.plan
                    self._work_item_tracker = WorkItemTracker(plan_result.plan)
                    phase = AgentPhase.EXECUTE
                elif self._plan_attempt <= self.max_plan_retries:
                    phase = AgentPhase.PLAN
                else:
                    phase = AgentPhase.FINALIZE
            elif phase is AgentPhase.EVALUATE:
                self._completion_attempt += 1
                work_item_report = None
                if self._work_item_tracker is not None:
                    work_item_report = self._work_item_tracker.update(
                        message.content or "", tuple(self.context_store.evidence)
                    )
                self._completion_report = evaluate_completion(
                    task_spec=self.task_spec,
                    changed_files=tuple(self._changed_files),
                    validation_plan=self._validation_plan,
                    validation_results=tuple(self._validation_results),
                    evidence=tuple(self.context_store.evidence),
                    evaluation_output=message.content or "",
                    attempt=self._completion_attempt,
                    max_retries=self.max_completion_retries,
                    execution_plan=self._execution_plan,
                    work_item_report=work_item_report,
                )
                phase_feedback = {"role": "system", "content": self._completion_report.format_for_prompt()}
                phase = self._phase_after_completion(self._completion_report)
            else:
                phase = self._next_phase(phase)
            validation_message = None
            if phase is AgentPhase.EVALUATE:
                validation_message = self._automatic_validation_message()
            messages = [
                *base_messages,
                {"role": "system", "content": self.context_store.format_for_prompt()},
            ]
            instruction_message = self._project_instruction_message(phase)
            if instruction_message is not None:
                messages.append(instruction_message)
            if self._work_item_tracker is not None:
                messages.append({"role": "system", "content": self._work_item_tracker.format_for_prompt()})
            if validation_message is not None:
                messages.append(validation_message)
            if phase_feedback is not None:
                messages.append(phase_feedback)
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
            validation_results=tuple(self._validation_results),
            phase_artifacts=tuple(self.context_store.phase_artifacts),
            evidence=tuple(self.context_store.evidence),
            execution_plan=self._execution_plan,
            work_items=self._work_items(),
            completion_report=self._completion_report,
            phase=phase,
        )

    def _append_automatic_validation(self, messages: list[dict[str, Any]]) -> None:
        messages.append(self._automatic_validation_message())

    def _automatic_validation_message(self) -> dict[str, str]:
        plan = build_validation_plan(self.repo_map, tuple(self._changed_files))
        self._validation_plan = plan
        results = execute_validation_plan(plan, self.workspace)
        self._validation_results[:] = results
        self.context_store.record_validation(AgentPhase.EVALUATE.value, results)
        if results:
            formatted_results = "\n".join(result.format_for_prompt() for result in results)
        else:
            formatted_results = "No validation commands were executed."
        return {
            "role": "system",
            "content": f"Automatic validation:\n{plan.format_for_prompt()}\nResults:\n{formatted_results}",
        }

    def _phase_message(self, phase: AgentPhase) -> dict[str, str]:
        instructions = {
            AgentPhase.UNDERSTAND: (
                "Inspect the repository evidence needed to understand the request. "
                "Do not modify files or run shell commands. When the request is understood, respond with a concise understanding."
            ),
            AgentPhase.PLAN: (
                "Create a short execution plan based on inspected evidence. Do not modify files or run shell commands. "
                "End with a [PLAN] JSON object containing 1-6 steps. Every step must have id, objective, target_files, "
                "acceptance_criteria, and validation_hint. target_files are files the step expects to change and must "
                "respect TaskSpec.allowed_write_paths. acceptance_criteria contains zero-based TaskSpec criterion indexes, "
                "and the complete plan must cover every criterion and explicit target file."
            ),
            AgentPhase.EXECUTE: (
                "Carry out the pending or incomplete work items using focused changes. Read before editing, use "
                "replace_text for existing files, and inspect each automatically returned diff."
            ),
            AgentPhase.EVALUATE: (
                "Review the automatic validation results, changed files, and diffs. Use read_file or git_diff for "
                "additional evidence, but do not modify files. If a fix is still required, start the phase summary "
                "with [NEEDS_CHANGES]. Always end with a [COMPLETION] JSON object containing work_items and criteria "
                "lists. Include every execution plan step id and acceptance criterion index exactly once with "
                "satisfied=true or false. Completed entries must cite exact source values from the EvidenceTable."
            ),
            AgentPhase.FINALIZE: (
                "Produce the final answer without calling tools. Summarize the result, changed files, validation, "
                "and remaining risks."
            ),
        }
        changed = ", ".join(self._changed_files) if self._changed_files else "none"
        validation = self._validation_status()
        return {
            "role": "system",
            "content": (
                f"Current phase: {phase.value}\nChanged files so far: {changed}\n"
                f"Validation status: {validation}\n{instructions[phase]}"
            ),
        }

    def _allowed_tools(self, phase: AgentPhase) -> set[str]:
        available = set(self.registry.names())
        if phase in {AgentPhase.UNDERSTAND, AgentPhase.PLAN}:
            return available.intersection(DISCOVERY_TOOLS)
        if phase is AgentPhase.EXECUTE:
            return available.intersection(self.task_spec.allowed_tools())
        if phase is AgentPhase.EVALUATE:
            return available.intersection(EVALUATION_TOOLS)
        return set()

    def _validation_status(self) -> str:
        if not self._validation_results:
            return "not run"
        return "passed" if all(result.passed for result in self._validation_results) else "failed"

    def _next_phase(self, phase: AgentPhase) -> AgentPhase:
        if phase is AgentPhase.UNDERSTAND:
            if self.task_spec.plan_required:
                return AgentPhase.PLAN
            if self.task_spec.write_allowed:
                return AgentPhase.EXECUTE
            return AgentPhase.EVALUATE
        if phase is AgentPhase.PLAN:
            return AgentPhase.EXECUTE if self.task_spec.write_allowed else AgentPhase.EVALUATE
        if phase is AgentPhase.EXECUTE:
            return AgentPhase.EVALUATE
        return AgentPhase.FINALIZE

    def _phase_after_completion(self, report: CompletionReport) -> AgentPhase:
        if report.ready or not report.retry_allowed or report.retry_phase is None:
            return AgentPhase.FINALIZE
        return AgentPhase(report.retry_phase)

    def _work_items(self) -> tuple[WorkItemState, ...]:
        if self._work_item_tracker is None:
            return ()
        return self._work_item_tracker.report.states

    def _project_instruction_message(self, phase: AgentPhase) -> dict[str, str] | None:
        if phase is AgentPhase.FINALIZE:
            return None
        bundle = self.project_instructions.resolve_for_paths(self._instruction_paths())
        if not bundle.documents:
            return None
        for document in bundle.documents:
            self.context_store.record_instruction(phase.value, document.source, document.scope)
        return {"role": "system", "content": bundle.format_for_prompt()}

    def _instruction_paths(self) -> tuple[str, ...]:
        paths = [*self.task_spec.target_files, *self._read_files, *self._changed_files]
        if self._execution_plan is not None:
            for step in self._execution_plan.steps:
                paths.extend(step.target_files)
        elif not self.task_spec.target_files:
            paths.extend(self.context_plan.must_read)
        return tuple(dict.fromkeys(path for path in paths if path))

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

    def _tool_arguments(self, tool_call: Any) -> dict[str, Any]:
        try:
            arguments = json.loads(tool_call.function.arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return arguments if isinstance(arguments, dict) else {}

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
