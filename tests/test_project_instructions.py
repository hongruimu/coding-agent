import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import ContextPlan, build_context_plan
from agent.core import AgentPhase, CodingAgent
from agent.project_instructions import build_project_instructions
from agent.task_spec import TaskSpec, TaskType, build_task_spec
from tools import create_registry
from tools.repo import build_repo_map


class InstructionClient:
    def __init__(self):
        self.read_requested = False
        self.messages_by_phase: dict[str, list[dict]] = {}

    def create(self, *, model, messages, tools):
        phase = _current_phase(messages)
        self.messages_by_phase.setdefault(phase, messages)
        if phase == AgentPhase.UNDERSTAND.value and not self.read_requested:
            self.read_requested = True
            function = SimpleNamespace(name="read_file", arguments=json.dumps({"path": "src/app.py"}))
            tool_call = SimpleNamespace(id="read-1", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        elif phase == AgentPhase.EVALUATE.value:
            message = SimpleNamespace(content=_completion_output(messages, "src/app.py"), tool_calls=None)
        else:
            message = SimpleNamespace(content=f"{phase} complete", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class PlannedTargetClient:
    def __init__(self):
        self.edited = False
        self.execute_messages: list[dict] = []

    def create(self, *, model, messages, tools):
        phase = _current_phase(messages)
        if phase == AgentPhase.PLAN.value:
            step = {
                "id": "step-1",
                "objective": "Update src/app.py.",
                "target_files": ["src/app.py"],
                "acceptance_criteria": [0],
                "validation_hint": "Run syntax validation.",
            }
            message = SimpleNamespace(content="[PLAN]\n" + json.dumps({"steps": [step]}), tool_calls=None)
        elif phase == AgentPhase.EXECUTE.value and not self.edited:
            self.execute_messages = messages
            self.edited = True
            function = SimpleNamespace(
                name="replace_text",
                arguments=json.dumps({"path": "src/app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}),
            )
            tool_call = SimpleNamespace(id="edit-1", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        elif phase == AgentPhase.EVALUATE.value:
            payload = {
                "work_items": [{"id": "step-1", "satisfied": True, "evidence": ["src/app.py"]}],
                "criteria": [{"index": 0, "satisfied": True, "evidence": ["src/app.py"]}],
            }
            message = SimpleNamespace(content="[COMPLETION]\n" + json.dumps(payload), tool_calls=None)
        else:
            message = SimpleNamespace(content=f"{phase} complete", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ProjectInstructionsTests(unittest.TestCase):
    def test_resolves_root_and_nested_instructions_for_target_scope(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._write(workspace, "PROJECT.md", "root project")
            self._write(workspace, "AGENTS.md", "root agents")
            self._write(workspace, "src/CLAUDE.md", "src claude")
            self._write(workspace, "src/AGENTS.md", "src agents")
            self._write(workspace, "tests/AGENTS.md", "tests agents")
            instructions = build_project_instructions(workspace, _all_files(workspace))

            bundle = instructions.resolve_for_paths(("src/app.py",))

            self.assertEqual(
                ("PROJECT.md", "AGENTS.md", "src/CLAUDE.md", "src/AGENTS.md"),
                tuple(document.source for document in bundle.documents),
            )
            self.assertNotIn("tests/AGENTS.md", tuple(document.source for document in bundle.documents))

    def test_same_directory_and_nested_precedence_are_deterministic(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            for name in ("AGENTS.md", "CLAUDE.md", "PROJECT.md"):
                self._write(workspace, name, name)
            for name in ("AGENTS.md", "CLAUDE.md", "PROJECT.md"):
                self._write(workspace, f"src/{name}", f"src {name}")
            instructions = build_project_instructions(workspace, _all_files(workspace))

            bundle = instructions.resolve_for_paths(("src/app.py",))

            self.assertEqual(
                (
                    "PROJECT.md",
                    "CLAUDE.md",
                    "AGENTS.md",
                    "src/PROJECT.md",
                    "src/CLAUDE.md",
                    "src/AGENTS.md",
                ),
                tuple(document.source for document in bundle.documents),
            )

    def test_unrelated_nested_rules_are_not_loaded(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._write(workspace, "AGENTS.md", "root")
            self._write(workspace, "src/AGENTS.md", "src")
            self._write(workspace, "tests/AGENTS.md", "tests")
            instructions = build_project_instructions(workspace, _all_files(workspace))

            bundle = instructions.resolve_for_paths(("tests/test_app.py",))

            self.assertEqual(("AGENTS.md", "tests/AGENTS.md"), tuple(item.source for item in bundle.documents))

    def test_file_and_total_budgets_truncate_instruction_content(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._write(workspace, "AGENTS.md", "a" * 100)
            self._write(workspace, "src/AGENTS.md", "b" * 100)
            instructions = build_project_instructions(
                workspace,
                _all_files(workspace),
                max_file_chars=40,
                max_total_chars=50,
            )

            bundle = instructions.resolve_for_paths(("src/app.py",))

            self.assertTrue(bundle.content_truncated)
            self.assertLessEqual(sum(len(item.content) for item in bundle.documents), 50)
            self.assertTrue(any(item.truncated for item in bundle.documents))

    def test_file_budget_keeps_root_and_most_specific_rules(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._write(workspace, "AGENTS.md", "root")
            self._write(workspace, "src/AGENTS.md", "src")
            self._write(workspace, "src/pkg/AGENTS.md", "pkg")
            instructions = build_project_instructions(
                workspace,
                _all_files(workspace),
                max_files=2,
            )

            bundle = instructions.resolve_for_paths(("src/pkg/app.py",))

            self.assertTrue(bundle.files_truncated)
            self.assertEqual(("AGENTS.md", "src/pkg/AGENTS.md"), tuple(item.source for item in bundle.documents))

    def test_agent_injects_scoped_rules_and_records_evidence(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._write(workspace, "AGENTS.md", "ROOT_RULE")
            self._write(workspace, "src/AGENTS.md", "SRC_RULE")
            self._write(workspace, "tests/AGENTS.md", "TEST_RULE")
            self._write(workspace, "src/app.py", "VALUE = 1\n")
            repository = build_repo_map(workspace=workspace)
            task_spec = build_task_spec("解释 src/app.py", workspace, repository)
            context_plan = build_context_plan(task_spec, repository)
            client = InstructionClient()
            agent = CodingAgent(
                client=client,
                registry=create_registry(workspace, allowed_tools=task_spec.allowed_tools()),
                model="test-model",
                workspace=workspace,
                task_spec=task_spec,
                context_plan=context_plan,
                repo_map=repository,
            )

            result = agent.run("解释 src/app.py")

            understand_context = "\n".join(
                str(message["content"]) for message in client.messages_by_phase["understand"]
            )
            self.assertLess(understand_context.index("ROOT_RULE"), understand_context.index("SRC_RULE"))
            self.assertNotIn("TEST_RULE", understand_context)
            instruction_sources = {item.source for item in result.evidence if item.kind == "instruction"}
            self.assertEqual({"AGENTS.md", "src/AGENTS.md"}, instruction_sources)

    def test_execution_plan_targets_load_nested_rules_before_execute(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._write(workspace, "AGENTS.md", "ROOT_RULE")
            self._write(workspace, "src/AGENTS.md", "SRC_RULE")
            self._write(workspace, "src/app.py", "VALUE = 1\n")
            repository = build_repo_map(workspace=workspace)
            task_spec = TaskSpec(
                raw_prompt="实现功能",
                workspace=workspace.resolve(),
                task_type=TaskType.MODIFY_CODE,
                target_files=(),
                allowed_write_paths=("**/*",),
                acceptance_criteria=("Implement the requested behavior.",),
                write_allowed=True,
                shell_allowed=True,
                plan_required=True,
            )
            context_plan = ContextPlan(
                repo_summary="test repository",
                must_read=(),
                maybe_read=(),
                forbidden=(),
                max_files=5,
                max_chars_per_file=2_000,
            )
            client = PlannedTargetClient()
            agent = CodingAgent(
                client=client,
                registry=create_registry(
                    workspace,
                    allowed_tools=task_spec.allowed_tools(),
                    allowed_write_paths=task_spec.allowed_write_paths,
                ),
                model="test-model",
                workspace=workspace,
                task_spec=task_spec,
                context_plan=context_plan,
                repo_map=repository,
            )

            result = agent.run("实现功能")

            execute_context = "\n".join(str(message["content"]) for message in client.execute_messages)
            self.assertIn("ROOT_RULE", execute_context)
            self.assertIn("SRC_RULE", execute_context)
            self.assertEqual("VALUE = 2\n", (workspace / "src/app.py").read_text())
            self.assertTrue(result.completion_report.ready)

    def _write(self, workspace: Path, path: str, content: str) -> None:
        target = workspace / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)


def _all_files(workspace: Path) -> list[str]:
    return sorted(path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file())


def _current_phase(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] == "system" and message["content"].startswith("Current phase:"):
            return message["content"].splitlines()[0].split(":", maxsplit=1)[1].strip()
    raise AssertionError("Current phase message was not found")


def _completion_output(messages: list[dict], source: str) -> str:
    for message in messages:
        content = message.get("content")
        if message.get("role") == "system" and isinstance(content, str) and content.startswith("Task specification:"):
            task_spec = json.loads(content.removeprefix("Task specification:\n"))
            criteria = [
                {"index": index, "satisfied": True, "evidence": [source]}
                for index, _ in enumerate(task_spec["acceptance_criteria"])
            ]
            return "[COMPLETION]\n" + json.dumps({"criteria": criteria})
    raise AssertionError("Task specification message was not found")


if __name__ == "__main__":
    unittest.main()
