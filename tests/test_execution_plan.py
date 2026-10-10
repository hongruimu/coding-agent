import json
import tempfile
import unittest
from pathlib import Path

from agent.context_store import EvidenceItem
from agent.execution_plan import (
    WorkItemStatus,
    WorkItemTracker,
    parse_execution_plan,
)
from agent.task_spec import TaskSpec, TaskType


class ExecutionPlanTests(unittest.TestCase):
    def test_parses_structured_plan_and_normalizes_targets(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            spec = self._spec(workspace, target_files=("src/app.py",))

            result = parse_execution_plan(
                _plan_output(
                    [
                        {
                            "id": "step-1",
                            "objective": "Update application behavior.",
                            "target_files": ["./src/app.py"],
                            "acceptance_criteria": [0, 1],
                            "validation_hint": "Run the related tests.",
                        }
                    ]
                ),
                spec,
            )

            self.assertTrue(result.ready)
            self.assertEqual(("src/app.py",), result.plan.steps[0].target_files)
            self.assertEqual((0, 1), result.plan.steps[0].acceptance_criteria)

    def test_rejects_plan_without_marker(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            result = parse_execution_plan("plain text", self._spec(Path(workspace_dir)))

            self.assertFalse(result.ready)
            self.assertIn("[PLAN]", result.errors[0])

    def test_rejects_duplicate_steps_and_uncovered_criteria(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir))
            output = _plan_output(
                [
                    {
                        "id": "step-1",
                        "objective": "First.",
                        "target_files": [],
                        "acceptance_criteria": [0],
                        "validation_hint": None,
                    },
                    {
                        "id": "step-1",
                        "objective": "Duplicate.",
                        "target_files": [],
                        "acceptance_criteria": [0],
                        "validation_hint": None,
                    },
                ]
            )

            result = parse_execution_plan(output, spec)

            self.assertFalse(result.ready)
            self.assertTrue(any("Duplicate" in error for error in result.errors))
            self.assertTrue(any("does not cover acceptance criteria" in error for error in result.errors))

    def test_rejects_targets_outside_workspace_or_write_allowance(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            spec = self._spec(workspace, target_files=("README.md",), allowed_write_paths=("README.md",))
            output = _plan_output(
                [
                    {
                        "id": "step-1",
                        "objective": "Write files.",
                        "target_files": ["../outside.md", "src/app.py"],
                        "acceptance_criteria": [0, 1],
                        "validation_hint": None,
                    }
                ]
            )

            result = parse_execution_plan(output, spec)

            self.assertFalse(result.ready)
            self.assertTrue(any("escapes the workspace" in error for error in result.errors))
            self.assertTrue(any("allowed_write_paths" in error for error in result.errors))

    def test_tracker_completes_work_items_with_known_evidence(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            plan = parse_execution_plan(
                _valid_plan_output(),
                self._spec(Path(workspace_dir), target_files=("app.py",)),
            ).plan
            tracker = WorkItemTracker(plan)
            evidence = (EvidenceItem("execute", "diff", "app.py", "reviewed", "success"),)

            report = tracker.update(_completion_output("step-1", True, "app.py"), evidence)

            self.assertTrue(report.ready)
            self.assertEqual(("step-1",), report.completed_ids)
            self.assertEqual(WorkItemStatus.COMPLETED, report.states[0].status)

    def test_tracker_marks_explicitly_unsatisfied_item_incomplete(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            plan = parse_execution_plan(
                _valid_plan_output(),
                self._spec(Path(workspace_dir), target_files=("app.py",)),
            ).plan
            tracker = WorkItemTracker(plan)

            report = tracker.update(_completion_output("step-1", False, None), ())

            self.assertEqual(("step-1",), report.incomplete_ids)
            self.assertEqual(WorkItemStatus.INCOMPLETE, report.states[0].status)

    def test_tracker_rejects_unknown_evidence(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            plan = parse_execution_plan(
                _valid_plan_output(),
                self._spec(Path(workspace_dir), target_files=("app.py",)),
            ).plan
            tracker = WorkItemTracker(plan)

            report = tracker.update(_completion_output("step-1", True, "missing.py"), ())

            self.assertFalse(report.ready)
            self.assertEqual(("step-1",), report.pending_ids)
            self.assertTrue(any("unknown evidence" in error for error in report.errors))

    def test_tracker_requires_every_plan_step_once(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            spec = self._spec(Path(workspace_dir), target_files=("app.py",))
            plan = parse_execution_plan(
                _plan_output(
                    [
                        {
                            "id": "step-1",
                            "objective": "Change code.",
                            "target_files": ["app.py"],
                            "acceptance_criteria": [0],
                            "validation_hint": None,
                        },
                        {
                            "id": "step-2",
                            "objective": "Validate code.",
                            "target_files": [],
                            "acceptance_criteria": [1],
                            "validation_hint": "Run tests.",
                        },
                    ]
                ),
                spec,
            ).plan
            tracker = WorkItemTracker(plan)

            evidence = (EvidenceItem("execute", "diff", "app.py", "reviewed", "success"),)
            report = tracker.update(_completion_output("step-1", True, "app.py"), evidence)

            self.assertFalse(report.ready)
            self.assertEqual(("step-2",), report.pending_ids)
            self.assertTrue(any("exactly once" in error for error in report.errors))

    def _spec(
        self,
        workspace: Path,
        *,
        target_files: tuple[str, ...] = (),
        allowed_write_paths: tuple[str, ...] = ("**/*",),
    ) -> TaskSpec:
        return TaskSpec(
            raw_prompt="test",
            workspace=workspace,
            task_type=TaskType.MODIFY_CODE,
            target_files=target_files,
            allowed_write_paths=allowed_write_paths,
            acceptance_criteria=("implement behavior", "validate behavior"),
            write_allowed=True,
            shell_allowed=True,
            plan_required=True,
        )


def _valid_plan_output() -> str:
    return _plan_output(
        [
            {
                "id": "step-1",
                "objective": "Implement and validate the change.",
                "target_files": ["app.py"],
                "acceptance_criteria": [0, 1],
                "validation_hint": "Run tests.",
            }
        ]
    )


def _plan_output(steps: list[dict]) -> str:
    return "[PLAN]\n" + json.dumps({"steps": steps})


def _completion_output(step_id: str, satisfied: bool, source: str | None) -> str:
    evidence = [source] if source is not None else []
    return "[COMPLETION]\n" + json.dumps(
        {"work_items": [{"id": step_id, "satisfied": satisfied, "evidence": evidence}]}
    )


if __name__ == "__main__":
    unittest.main()
