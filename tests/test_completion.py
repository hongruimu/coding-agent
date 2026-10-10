import json
import tempfile
import unittest
from pathlib import Path

from agent.completion import evaluate_completion
from agent.context_store import EvidenceItem
from agent.task_spec import TaskSpec, TaskType
from agent.validation import ValidationCommand, ValidationPlan, ValidationResult


class CompletionGateTests(unittest.TestCase):
    def test_ready_write_task_passes_all_checks(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=True, target_files=("app.py",))
            plan, results = _validation(passed=True)
            evidence = (
                EvidenceItem("execute", "change", "app.py", "changed", "success"),
                EvidenceItem("execute", "diff", "app.py", "diff", "success"),
                EvidenceItem("evaluate", "validation", "syntax", "passed", "passed"),
            )

            report = evaluate_completion(
                task_spec=spec,
                changed_files=("app.py",),
                validation_plan=plan,
                validation_results=results,
                evidence=evidence,
                evaluation_output=_checklist(spec, "app.py"),
                attempt=1,
                max_retries=2,
            )

            self.assertTrue(report.ready)
            self.assertIsNone(report.retry_phase)
            self.assertEqual(spec.acceptance_criteria, report.satisfied_criteria)

    def test_write_task_without_changes_retries_execute(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=True)
            evidence = (EvidenceItem("understand", "repository", ".", "map", "success"),)

            report = self._evaluate(spec, (), evidence, _checklist(spec, "."))

            self.assertFalse(report.ready)
            self.assertEqual("execute", report.retry_phase)

    def test_failed_validation_retries_execute(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=True)
            plan, results = _validation(passed=False)
            evidence = (EvidenceItem("execute", "diff", "app.py", "diff", "success"),)

            report = self._evaluate(
                spec,
                ("app.py",),
                evidence,
                _checklist(spec, "app.py"),
                validation_plan=plan,
                validation_results=results,
            )

            self.assertEqual("execute", report.retry_phase)
            self.assertIn("Required validation failed.", report.failed_checks)

    def test_read_only_task_without_discovery_evidence_retries_understand(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=False, acceptance_criteria=())

            report = self._evaluate(spec, (), (), "[COMPLETION]\n{\"criteria\": []}")

            self.assertEqual("understand", report.retry_phase)

    def test_missing_checklist_retries_evaluate(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=False)
            evidence = (EvidenceItem("understand", "file", "app.py", "read", "success"),)

            report = self._evaluate(spec, (), evidence, "Evaluation complete.")

            self.assertEqual("evaluate", report.retry_phase)

    def test_unknown_checklist_evidence_retries_evaluate(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=False)
            evidence = (EvidenceItem("understand", "file", "app.py", "read", "success"),)

            report = self._evaluate(spec, (), evidence, _checklist(spec, "missing.py"))

            self.assertEqual("evaluate", report.retry_phase)
            self.assertIn("missing.py", report.missing_evidence)

    def test_needs_changes_marker_retries_execute(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=True)
            evidence = (EvidenceItem("execute", "diff", "app.py", "diff", "success"),)
            output = "[NEEDS_CHANGES] fix behavior\n" + _checklist(spec, "app.py")

            report = self._evaluate(spec, ("app.py",), evidence, output)

            self.assertEqual("execute", report.retry_phase)

    def test_retry_limit_returns_incomplete_report(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), write_allowed=False)
            evidence = (EvidenceItem("understand", "file", "app.py", "read", "success"),)

            report = self._evaluate(spec, (), evidence, "missing", attempt=3, max_retries=2)

            self.assertFalse(report.ready)
            self.assertFalse(report.retry_allowed)
            self.assertIsNone(report.retry_phase)

    def test_readme_task_requires_readme_target_change(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            spec = self._spec(
                workspace,
                write_allowed=True,
                task_type=TaskType.CREATE_README,
                target_files=("README.md",),
            )
            evidence = (EvidenceItem("execute", "diff", "notes.md", "diff", "success"),)

            report = self._evaluate(spec, ("notes.md",), evidence, _checklist(spec, "notes.md"))

            self.assertEqual("execute", report.retry_phase)
            self.assertTrue(any("README" in check for check in report.failed_checks))

    def _evaluate(
        self,
        spec: TaskSpec,
        changed_files: tuple[str, ...],
        evidence: tuple[EvidenceItem, ...],
        output: str,
        *,
        validation_plan: ValidationPlan | None = None,
        validation_results: tuple[ValidationResult, ...] = (),
        attempt: int = 1,
        max_retries: int = 2,
    ):
        return evaluate_completion(
            task_spec=spec,
            changed_files=changed_files,
            validation_plan=validation_plan,
            validation_results=validation_results,
            evidence=evidence,
            evaluation_output=output,
            attempt=attempt,
            max_retries=max_retries,
        )

    def _spec(
        self,
        workspace: Path,
        *,
        write_allowed: bool,
        task_type: TaskType | None = None,
        target_files: tuple[str, ...] = (),
        acceptance_criteria: tuple[str, ...] = ("criterion one", "criterion two"),
    ) -> TaskSpec:
        return TaskSpec(
            raw_prompt="test",
            workspace=workspace,
            task_type=task_type or (TaskType.MODIFY_CODE if write_allowed else TaskType.EXPLAIN_PROJECT),
            target_files=target_files,
            allowed_write_paths=("**/*",) if write_allowed else (),
            acceptance_criteria=acceptance_criteria,
            write_allowed=write_allowed,
            shell_allowed=write_allowed,
            plan_required=write_allowed,
        )


def _checklist(spec: TaskSpec, source: str) -> str:
    criteria = [
        {"index": index, "satisfied": True, "evidence": [source]}
        for index, _ in enumerate(spec.acceptance_criteria)
    ]
    return "[COMPLETION]\n" + json.dumps({"criteria": criteria})


def _validation(*, passed: bool) -> tuple[ValidationPlan, tuple[ValidationResult, ...]]:
    command = ValidationCommand("syntax", ("python", "-m", "compileall"))
    plan = ValidationPlan((command,), "test", True)
    result = ValidationResult(
        name="syntax",
        argv=command.argv,
        exit_code=0 if passed else 1,
        stdout="",
        stderr="",
    )
    return plan, (result,)


if __name__ == "__main__":
    unittest.main()
