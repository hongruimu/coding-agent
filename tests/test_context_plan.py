import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import ContextPlan, build_context_plan
from agent.core import CodingAgent
from agent.task_spec import build_task_spec
from tools import create_registry
from tools.repo import build_repo_map


class ContextPlanTests(unittest.TestCase):
    def _build(self, workspace: Path, prompt: str):
        repository = build_repo_map(workspace=workspace)
        task_spec = build_task_spec(prompt, workspace, repository)
        return build_context_plan(task_spec, repository)

    def test_readme_plan_prioritizes_project_evidence(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "pyproject.toml").write_text("[project]\nname = 'demo'")
            (workspace / "README.md").write_text("# Old")
            (workspace / "src" / "main.py").write_text("print('demo')")
            (workspace / "tests" / "test_main.py").write_text("def test_main(): pass")

            plan = self._build(workspace, "更新 README.md")

            self.assertIn("pyproject.toml", plan.must_read)
            self.assertIn("README.md", plan.must_read)
            self.assertIn("src/main.py", plan.must_read)
            self.assertIn("tests/test_main.py", plan.maybe_read)
            self.assertLessEqual(len(plan.must_read) + len(plan.maybe_read), plan.max_files)

    def test_fix_test_plan_links_test_to_source_file(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "src" / "user.py").write_text("def get_user(): pass")
            (workspace / "tests" / "test_user.py").write_text("def test_user(): pass")

            plan = self._build(workspace, "修复测试 tests/test_user.py")

            self.assertEqual("tests/test_user.py", plan.must_read[0])
            self.assertIn("src/user.py", plan.must_read)

    def test_explain_target_is_read_before_general_context(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "agent").mkdir()
            (workspace / "agent" / "core.py").write_text("class CodingAgent: pass")
            (workspace / "agent" / "other.py").write_text("VALUE = 1")

            plan = self._build(workspace, "解释 agent/core.py 的实现")

            self.assertEqual("agent/core.py", plan.must_read[0])
            self.assertNotIn("agent/core.py", plan.maybe_read)

    def test_plan_excludes_ignored_files_and_has_fixed_budget(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / ".venv").mkdir()
            (workspace / ".venv" / "ignored.py").write_text("ignored")
            for index in range(20):
                (workspace / f"module_{index}.py").write_text("pass")

            plan = self._build(workspace, "解释项目架构")
            planned_files = plan.must_read + plan.maybe_read

            self.assertLessEqual(len(planned_files), plan.max_files)
            self.assertFalse(any(path.startswith(".venv/") for path in planned_files))
            self.assertIn(".venv/", plan.forbidden)
            self.assertEqual(12_000, plan.max_chars_per_file)

    def test_agent_enforces_file_count_and_character_budgets(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "one.txt").write_text("123456")
            (workspace / "two.txt").write_text("second")
            (workspace / ".env").write_text("SECRET=value")
            repository = build_repo_map(workspace=workspace)
            task_spec = build_task_spec("解释项目", workspace, repository)
            context_plan = ContextPlan(
                repo_summary="test",
                must_read=("one.txt",),
                maybe_read=(),
                forbidden=(".env",),
                max_files=1,
                max_chars_per_file=4,
            )
            agent = CodingAgent(
                client=SimpleNamespace(),
                registry=create_registry(
                    workspace,
                    allowed_tools=task_spec.allowed_tools(),
                    max_read_chars=context_plan.max_chars_per_file,
                ),
                model="test-model",
                workspace=workspace,
                task_spec=task_spec,
                context_plan=context_plan,
            )

            first_result = agent._execute_tool(_tool_call("read_file", {"path": "one.txt"}))
            repeated_result = agent._execute_tool(_tool_call("read_file", {"path": "./one.txt"}))
            blocked_result = agent._execute_tool(_tool_call("read_file", {"path": "two.txt"}))
            forbidden_result = agent._execute_tool(_tool_call("read_file", {"path": ".env"}))

            self.assertIn("1234", first_result)
            self.assertIn("truncated after 4 characters", first_result)
            self.assertNotIn("blocked", repeated_result)
            self.assertIn("context file budget of 1", blocked_result)
            self.assertIn("path is forbidden", forbidden_result)


def _tool_call(name: str, arguments: dict):
    import json

    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


if __name__ == "__main__":
    unittest.main()
