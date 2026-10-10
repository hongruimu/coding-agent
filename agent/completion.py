import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.context_store import EvidenceItem
from agent.execution_plan import ExecutionPlan, WorkItemReport
from agent.task_spec import TaskSpec, TaskType
from agent.validation import ValidationPlan, ValidationResult


COMPLETION_MARKER = "[COMPLETION]"
NEEDS_CHANGES_MARKER = "[needs_changes]"
DISCOVERY_EVIDENCE_KINDS = frozenset({"repository", "file", "search"})


@dataclass(frozen=True)
class CompletionReport:
    ready: bool
    failed_checks: tuple[str, ...]
    satisfied_criteria: tuple[str, ...]
    missing_evidence: tuple[str, ...]
    completed_work_items: tuple[str, ...]
    incomplete_work_items: tuple[str, ...]
    retry_phase: str | None
    attempt: int
    retry_allowed: bool

    def format_for_prompt(self) -> str:
        payload = {
            "ready": self.ready,
            "failed_checks": list(self.failed_checks),
            "satisfied_criteria": list(self.satisfied_criteria),
            "missing_evidence": list(self.missing_evidence),
            "completed_work_items": list(self.completed_work_items),
            "incomplete_work_items": list(self.incomplete_work_items),
            "retry_phase": self.retry_phase,
            "attempt": self.attempt,
            "retry_allowed": self.retry_allowed,
        }
        return "Completion gate:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def evaluate_completion(
    *,
    task_spec: TaskSpec,
    changed_files: tuple[str, ...],
    validation_plan: ValidationPlan | None,
    validation_results: tuple[ValidationResult, ...],
    evidence: tuple[EvidenceItem, ...],
    evaluation_output: str,
    attempt: int,
    max_retries: int,
    execution_plan: ExecutionPlan | None = None,
    work_item_report: WorkItemReport | None = None,
) -> CompletionReport:
    failed_checks: list[str] = []
    missing_evidence: list[str] = []
    implementation_failed = False
    understanding_failed = False
    evaluation_failed = False

    successful_evidence = tuple(item for item in evidence if item.status not in {"failed", "blocked"})
    normalized_changes = {_normalize_path(path, task_spec.workspace) for path in changed_files}

    if evaluation_output.lstrip().casefold().startswith(NEEDS_CHANGES_MARKER):
        failed_checks.append("The evaluator requested implementation changes.")
        implementation_failed = True

    if task_spec.write_allowed:
        if not normalized_changes:
            failed_checks.append("The task allows writes but no files were changed.")
            implementation_failed = True

        missing_targets = tuple(
            target
            for target in task_spec.target_files
            if _normalize_path(target, task_spec.workspace) not in normalized_changes
        )
        if missing_targets:
            failed_checks.append("Target files were not changed: " + ", ".join(missing_targets))
            missing_evidence.extend(missing_targets)
            implementation_failed = True

        if task_spec.task_type is TaskType.CREATE_README:
            readme_targets = tuple(
                target
                for target in task_spec.target_files
                if Path(target).name.casefold().startswith("readme")
            )
            if not readme_targets or not any(
                _normalize_path(target, task_spec.workspace) in normalized_changes for target in readme_targets
            ):
                failed_checks.append("The README task did not change its README target.")
                implementation_failed = True

        diff_sources = {
            _normalize_path(item.source, task_spec.workspace)
            for item in successful_evidence
            if item.kind == "diff"
        }
        missing_diffs = tuple(
            path for path in changed_files if _normalize_path(path, task_spec.workspace) not in diff_sources
        )
        if missing_diffs:
            failed_checks.append("Changed files lack diff evidence: " + ", ".join(missing_diffs))
            missing_evidence.extend(missing_diffs)
            implementation_failed = True

    if validation_plan is not None and validation_plan.required:
        expected_names = {command.name for command in validation_plan.commands}
        result_names = {result.name for result in validation_results}
        if not validation_results or not expected_names.issubset(result_names):
            failed_checks.append("Required validation results are missing.")
            missing_evidence.extend(sorted(expected_names - result_names))
            implementation_failed = True
        elif any(not result.passed for result in validation_results):
            failed_checks.append("Required validation failed.")
            implementation_failed = True

    if not task_spec.write_allowed and not any(
        item.kind in DISCOVERY_EVIDENCE_KINDS for item in successful_evidence
    ):
        failed_checks.append("The read-only task lacks repository, file, or search evidence.")
        missing_evidence.append("repository understanding evidence")
        understanding_failed = True

    completed_work_items: tuple[str, ...] = ()
    incomplete_work_items: tuple[str, ...] = ()
    if execution_plan is not None:
        expected_work_items = tuple(step.id for step in execution_plan.steps)
        if work_item_report is None:
            failed_checks.append("The evaluation output is missing work item completion status.")
            incomplete_work_items = expected_work_items
            evaluation_failed = True
        else:
            completed_work_items = work_item_report.completed_ids
            incomplete_work_items = tuple(
                step_id for step_id in expected_work_items if step_id not in set(completed_work_items)
            )
            if work_item_report.errors:
                failed_checks.extend(work_item_report.errors)
                missing_evidence.extend(work_item_report.pending_ids)
                evaluation_failed = True
            if work_item_report.incomplete_ids:
                failed_checks.append(
                    "Execution plan work items are incomplete: " + ", ".join(work_item_report.incomplete_ids)
                )
                implementation_failed = True
            elif incomplete_work_items and not work_item_report.errors:
                failed_checks.append(
                    "Execution plan work items lack completion evidence: " + ", ".join(incomplete_work_items)
                )
                missing_evidence.extend(incomplete_work_items)
                evaluation_failed = True

    criteria, parse_error = _parse_completion_criteria(evaluation_output)
    satisfied_criteria: list[str] = []
    known_sources = {item.source.strip().casefold() for item in successful_evidence if item.source.strip()}
    if parse_error is not None:
        failed_checks.append(parse_error)
        evaluation_failed = True
    else:
        assert criteria is not None
        expected_indexes = set(range(len(task_spec.acceptance_criteria)))
        indexes = [item.get("index") for item in criteria]
        valid_indexes = {index for index in indexes if isinstance(index, int) and not isinstance(index, bool)}
        if len(indexes) != len(valid_indexes) or valid_indexes != expected_indexes:
            failed_checks.append("The completion checklist must contain each acceptance criterion exactly once.")
            evaluation_failed = True

        for item in criteria:
            index = item.get("index")
            if not isinstance(index, int) or isinstance(index, bool) or index not in expected_indexes:
                continue
            criterion = task_spec.acceptance_criteria[index]
            if item.get("satisfied") is not True:
                failed_checks.append(f"Acceptance criterion {index} is not satisfied: {criterion}")
                evaluation_failed = True
                continue
            cited_sources = item.get("evidence")
            if not isinstance(cited_sources, list) or not cited_sources or not all(
                isinstance(source, str) and source.strip() for source in cited_sources
            ):
                failed_checks.append(f"Acceptance criterion {index} has no evidence sources.")
                missing_evidence.append(criterion)
                evaluation_failed = True
                continue
            unknown_sources = tuple(
                source for source in cited_sources if source.strip().casefold() not in known_sources
            )
            if unknown_sources:
                failed_checks.append(
                    f"Acceptance criterion {index} cites unknown evidence: " + ", ".join(unknown_sources)
                )
                missing_evidence.extend(unknown_sources)
                evaluation_failed = True
                continue
            satisfied_criteria.append(criterion)

    ready = not failed_checks
    retry_phase = None
    if not ready:
        if implementation_failed:
            retry_phase = "execute"
        elif understanding_failed:
            retry_phase = "understand"
        elif evaluation_failed:
            retry_phase = "evaluate"

    retry_allowed = not ready and attempt <= max_retries
    if not retry_allowed:
        retry_phase = None
    return CompletionReport(
        ready=ready,
        failed_checks=tuple(dict.fromkeys(failed_checks)),
        satisfied_criteria=tuple(dict.fromkeys(satisfied_criteria)),
        missing_evidence=tuple(dict.fromkeys(missing_evidence)),
        completed_work_items=completed_work_items,
        incomplete_work_items=incomplete_work_items,
        retry_phase=retry_phase,
        attempt=attempt,
        retry_allowed=retry_allowed,
    )


def _parse_completion_criteria(output: str) -> tuple[list[dict[str, Any]] | None, str | None]:
    marker_index = output.find(COMPLETION_MARKER)
    if marker_index < 0:
        return None, "The evaluation output is missing a [COMPLETION] checklist."

    payload = output[marker_index + len(COMPLETION_MARKER) :].lstrip()
    try:
        parsed, _ = json.JSONDecoder().raw_decode(payload)
    except json.JSONDecodeError:
        return None, "The [COMPLETION] checklist is not valid JSON."
    if not isinstance(parsed, dict) or not isinstance(parsed.get("criteria"), list):
        return None, "The [COMPLETION] checklist must contain a criteria list."
    if not all(isinstance(item, dict) for item in parsed["criteria"]):
        return None, "Every completion criterion must be a JSON object."
    return parsed["criteria"], None


def _normalize_path(path: str, workspace: Path) -> str:
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        try:
            candidate = candidate.resolve().relative_to(workspace)
        except ValueError:
            return candidate.as_posix().casefold()
    normalized = candidate.as_posix()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.casefold()
