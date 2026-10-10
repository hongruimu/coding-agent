import fnmatch
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from agent.context_store import EvidenceItem
from agent.task_spec import TaskSpec


PLAN_MARKER = "[PLAN]"
COMPLETION_MARKER = "[COMPLETION]"
MAX_PLAN_STEPS = 6


@dataclass(frozen=True)
class PlanStep:
    id: str
    objective: str
    target_files: tuple[str, ...]
    acceptance_criteria: tuple[int, ...]
    validation_hint: str | None = None


@dataclass(frozen=True)
class ExecutionPlan:
    steps: tuple[PlanStep, ...]

    def format_for_prompt(self) -> str:
        payload = {
            "steps": [
                {
                    "id": step.id,
                    "objective": step.objective,
                    "target_files": list(step.target_files),
                    "acceptance_criteria": list(step.acceptance_criteria),
                    "validation_hint": step.validation_hint,
                }
                for step in self.steps
            ]
        }
        return "Execution plan:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class PlanParseResult:
    plan: ExecutionPlan | None
    errors: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return self.plan is not None and not self.errors

    def format_for_prompt(self) -> str:
        payload = {
            "accepted": self.ready,
            "errors": list(self.errors),
        }
        return "Execution plan review:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


class WorkItemStatus(str, Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class WorkItemState:
    step_id: str
    status: WorkItemStatus
    evidence: tuple[str, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class WorkItemReport:
    states: tuple[WorkItemState, ...]
    errors: tuple[str, ...]
    attempt: int

    @property
    def completed_ids(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in self.states if item.status is WorkItemStatus.COMPLETED)

    @property
    def incomplete_ids(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in self.states if item.status is WorkItemStatus.INCOMPLETE)

    @property
    def pending_ids(self) -> tuple[str, ...]:
        return tuple(item.step_id for item in self.states if item.status is WorkItemStatus.PENDING)

    @property
    def ready(self) -> bool:
        return not self.errors and bool(self.states) and len(self.completed_ids) == len(self.states)

    def format_for_prompt(self) -> str:
        payload = {
            "attempt": self.attempt,
            "ready": self.ready,
            "errors": list(self.errors),
            "states": [
                {
                    "id": item.step_id,
                    "status": item.status.value,
                    "evidence": list(item.evidence),
                    "detail": item.detail,
                }
                for item in self.states
            ],
        }
        return "Work item tracker:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


class WorkItemTracker:
    def __init__(self, plan: ExecutionPlan):
        self.plan = plan
        self._attempt = 0
        self._report = WorkItemReport(
            states=tuple(WorkItemState(step.id, WorkItemStatus.PENDING) for step in plan.steps),
            errors=(),
            attempt=0,
        )

    @property
    def report(self) -> WorkItemReport:
        return self._report

    def update(self, evaluation_output: str, evidence: tuple[EvidenceItem, ...]) -> WorkItemReport:
        self._attempt += 1
        successful_sources = {
            item.source.strip().casefold()
            for item in evidence
            if item.status not in {"failed", "blocked"} and item.source.strip()
        }
        payload, parse_error = _parse_marked_object(evaluation_output, COMPLETION_MARKER)
        if parse_error is not None:
            return self._store_pending((parse_error,))

        assert payload is not None
        work_items = payload.get("work_items")
        if not isinstance(work_items, list) or not all(isinstance(item, dict) for item in work_items):
            return self._store_pending(("The [COMPLETION] checklist must contain a work_items list.",))

        expected_ids = tuple(step.id for step in self.plan.steps)
        expected_id_set = set(expected_ids)
        submitted_ids = [item.get("id") for item in work_items]
        valid_ids = [item_id for item_id in submitted_ids if isinstance(item_id, str) and item_id]
        errors: list[str] = []
        if len(valid_ids) != len(set(valid_ids)) or set(valid_ids) != expected_id_set:
            errors.append("The work item checklist must contain every execution plan step exactly once.")

        submitted_by_id = {
            item["id"]: item
            for item in work_items
            if isinstance(item.get("id"), str) and item["id"] in expected_id_set
        }
        states: list[WorkItemState] = []
        for step in self.plan.steps:
            item = submitted_by_id.get(step.id)
            if item is None:
                states.append(WorkItemState(step.id, WorkItemStatus.PENDING, detail="Missing from checklist."))
                continue

            satisfied = item.get("satisfied")
            if not isinstance(satisfied, bool):
                errors.append(f"Work item {step.id} must provide a boolean satisfied value.")
                states.append(WorkItemState(step.id, WorkItemStatus.PENDING, detail="Invalid satisfied value."))
                continue

            cited_sources = item.get("evidence")
            if satisfied is False:
                detail = item.get("detail") if isinstance(item.get("detail"), str) else ""
                states.append(WorkItemState(step.id, WorkItemStatus.INCOMPLETE, detail=detail))
                continue

            if not isinstance(cited_sources, list) or not cited_sources or not all(
                isinstance(source, str) and source.strip() for source in cited_sources
            ):
                errors.append(f"Completed work item {step.id} must cite evidence sources.")
                states.append(WorkItemState(step.id, WorkItemStatus.PENDING, detail="Evidence is missing."))
                continue

            unknown_sources = tuple(
                source for source in cited_sources if source.strip().casefold() not in successful_sources
            )
            if unknown_sources:
                errors.append(f"Work item {step.id} cites unknown evidence: " + ", ".join(unknown_sources))
                states.append(
                    WorkItemState(
                        step.id,
                        WorkItemStatus.PENDING,
                        evidence=tuple(cited_sources),
                        detail="Evidence is not present in the EvidenceTable.",
                    )
                )
                continue
            states.append(WorkItemState(step.id, WorkItemStatus.COMPLETED, tuple(cited_sources)))

        self._report = WorkItemReport(tuple(states), tuple(dict.fromkeys(errors)), self._attempt)
        return self._report

    def format_for_prompt(self) -> str:
        return f"{self.plan.format_for_prompt()}\n{self._report.format_for_prompt()}"

    def _store_pending(self, errors: tuple[str, ...]) -> WorkItemReport:
        self._report = WorkItemReport(
            states=tuple(WorkItemState(step.id, WorkItemStatus.PENDING) for step in self.plan.steps),
            errors=errors,
            attempt=self._attempt,
        )
        return self._report


def parse_execution_plan(
    output: str,
    task_spec: TaskSpec,
    *,
    max_steps: int = MAX_PLAN_STEPS,
) -> PlanParseResult:
    payload, parse_error = _parse_marked_object(output, PLAN_MARKER)
    if parse_error is not None:
        return PlanParseResult(None, (parse_error,))

    assert payload is not None
    raw_steps = payload.get("steps")
    if not isinstance(raw_steps, list):
        return PlanParseResult(None, ("The [PLAN] object must contain a steps list.",))
    if not raw_steps:
        return PlanParseResult(None, ("The execution plan must contain at least one step.",))
    if len(raw_steps) > max_steps:
        return PlanParseResult(None, (f"The execution plan exceeds the limit of {max_steps} steps.",))

    errors: list[str] = []
    parsed_steps: list[PlanStep] = []
    seen_ids: set[str] = set()
    covered_criteria: set[int] = set()
    planned_targets: set[str] = set()
    for index, raw_step in enumerate(raw_steps):
        if not isinstance(raw_step, dict):
            errors.append(f"Plan step {index} must be a JSON object.")
            continue

        step_id = raw_step.get("id")
        objective = raw_step.get("objective")
        if not isinstance(step_id, str) or not step_id.strip():
            errors.append(f"Plan step {index} must have a non-empty id.")
            continue
        step_id = step_id.strip()
        if step_id in seen_ids:
            errors.append(f"Duplicate plan step id: {step_id}")
            continue
        seen_ids.add(step_id)
        if not isinstance(objective, str) or not objective.strip():
            errors.append(f"Plan step {step_id} must have a non-empty objective.")
            continue

        target_files, target_errors = _parse_target_files(raw_step.get("target_files"), task_spec)
        errors.extend(f"Plan step {step_id}: {error}" for error in target_errors)
        planned_targets.update(target_files)

        criteria, criteria_errors = _parse_criteria(raw_step.get("acceptance_criteria"), task_spec)
        errors.extend(f"Plan step {step_id}: {error}" for error in criteria_errors)
        covered_criteria.update(criteria)

        validation_hint = raw_step.get("validation_hint")
        if validation_hint is not None and not isinstance(validation_hint, str):
            errors.append(f"Plan step {step_id}: validation_hint must be a string or null.")
            validation_hint = None
        parsed_steps.append(
            PlanStep(
                id=step_id,
                objective=objective.strip(),
                target_files=target_files,
                acceptance_criteria=criteria,
                validation_hint=validation_hint.strip() if isinstance(validation_hint, str) else None,
            )
        )

    expected_criteria = set(range(len(task_spec.acceptance_criteria)))
    missing_criteria = sorted(expected_criteria - covered_criteria)
    if missing_criteria:
        errors.append("The execution plan does not cover acceptance criteria: " + ", ".join(map(str, missing_criteria)))

    required_targets = {_normalize_target(path, task_spec.workspace) for path in task_spec.target_files}
    missing_targets = sorted(path for path in required_targets if path is not None and path not in planned_targets)
    if task_spec.write_allowed and missing_targets:
        errors.append("The execution plan does not cover target files: " + ", ".join(missing_targets))

    if errors:
        return PlanParseResult(None, tuple(dict.fromkeys(errors)))
    return PlanParseResult(ExecutionPlan(tuple(parsed_steps)), ())


def _parse_target_files(value: Any, task_spec: TaskSpec) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(value, list) or not all(isinstance(path, str) and path.strip() for path in value):
        return (), ("target_files must be a list of workspace-relative file paths.",)

    targets: list[str] = []
    errors: list[str] = []
    for path in value:
        normalized = _normalize_target(path, task_spec.workspace)
        if normalized is None:
            errors.append(f"target file escapes the workspace: {path}")
            continue
        if task_spec.write_allowed and not _is_write_allowed(normalized, task_spec.allowed_write_paths):
            errors.append(f"target file is outside TaskSpec.allowed_write_paths: {normalized}")
            continue
        targets.append(normalized)
    return tuple(dict.fromkeys(targets)), tuple(errors)


def _parse_criteria(value: Any, task_spec: TaskSpec) -> tuple[tuple[int, ...], tuple[str, ...]]:
    if not isinstance(value, list) or not value:
        return (), ("acceptance_criteria must contain at least one criterion index.",)
    if not all(isinstance(index, int) and not isinstance(index, bool) for index in value):
        return (), ("acceptance_criteria must contain integer indexes.",)

    criteria = tuple(dict.fromkeys(value))
    maximum = len(task_spec.acceptance_criteria)
    invalid = tuple(index for index in criteria if index < 0 or index >= maximum)
    if invalid:
        return (), ("acceptance_criteria contains invalid indexes: " + ", ".join(map(str, invalid)),)
    return criteria, ()


def _normalize_target(path: str, workspace: Path) -> str | None:
    root = workspace.expanduser().resolve()
    candidate = Path(path).expanduser()
    absolute = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    try:
        relative = absolute.relative_to(root)
    except ValueError:
        return None
    normalized = relative.as_posix()
    return normalized if normalized not in {"", "."} else None


def _is_write_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    if "*" in allowed_paths or "**/*" in allowed_paths:
        return True
    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in allowed_paths)


def _parse_marked_object(output: str, marker: str) -> tuple[dict[str, Any] | None, str | None]:
    marker_index = output.find(marker)
    if marker_index < 0:
        return None, f"The output is missing a {marker} JSON object."

    payload = output[marker_index + len(marker) :].lstrip()
    try:
        parsed, _ = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError:
        return None, f"The {marker} object is not valid JSON."
    if not isinstance(parsed, dict):
        return None, f"The {marker} payload must be a JSON object."
    return parsed, None
