import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import build_context_plan
from agent.core import CodingAgent
from agent.task_spec import TaskType, build_task_spec
from tools import create_registry
from tools.filesystem import write_file
from tools.repo import build_repo_map


class RecordingClient:
    def __init__(self):
        self.messages = []

    def create(self, *, model, messages, tools):
        self.messages = messages
        message = SimpleNamespace(content="done", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class TaskSpecTests(unittest.TestCase):
    def _build(self, workspace: Path, prompt: str):
        return build_task_spec(prompt, workspace, build_repo_map(workspace=workspace))

    def test_create_readme_only_allows_readme_write(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            spec = self._build(workspace, "为这个项目创建 README.md")

            self.assertEqual(TaskType.CREATE_README, spec.task_type)
            self.assertEqual(("README.md",), spec.target_files)
            self.assertEqual(("README.md",), spec.allowed_write_paths)
            self.assertTrue(spec.write_allowed)
            self.assertFalse(spec.shell_allowed)
            self.assertEqual(
                ["grep_code", "list_files", "read_file", "repo_map", "search_files", "write_file"],
                sorted(spec.allowed_tools()),
            )

    def test_explain_task_is_read_only(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "agent.py").write_text("print('demo')")

            spec = self._build(workspace, "解释 agent.py 的实现逻辑")

            self.assertEqual(TaskType.EXPLAIN_PROJECT, spec.task_type)
            self.assertEqual(("agent.py",), spec.target_files)
            self.assertFalse(spec.write_allowed)
            self.assertFalse(spec.shell_allowed)
            self.assertNotIn("write_file", spec.allowed_tools())
            self.assertNotIn("run_command", spec.allowed_tools())

    def test_fix_test_extracts_target_and_allows_shell(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "tests").mkdir()
            (workspace / "tests" / "test_user.py").write_text("def test_user(): pass")

            spec = self._build(workspace, "修复测试 tests/test_user.py")

            self.assertEqual(TaskType.FIX_TEST, spec.task_type)
            self.assertEqual(("tests/test_user.py",), spec.target_files)
            self.assertEqual(("tests/test_user.py",), spec.allowed_write_paths)
            self.assertTrue(spec.write_allowed)
            self.assertTrue(spec.shell_allowed)
            self.assertIn("run_command", spec.allowed_tools())

    def test_modify_task_without_target_allows_workspace_writes(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            spec = self._build(workspace, "实现用户登录功能")

            self.assertEqual(TaskType.MODIFY_CODE, spec.task_type)
            self.assertEqual(("**/*",), spec.allowed_write_paths)
            self.assertTrue(spec.write_allowed)
            self.assertTrue(spec.shell_allowed)
            self.assertTrue(spec.plan_required)

    def test_unknown_task_is_read_only(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            spec = self._build(workspace, "你好")

            self.assertEqual(TaskType.UNKNOWN, spec.task_type)
            self.assertFalse(spec.write_allowed)
            self.assertFalse(spec.shell_allowed)

    def test_registry_filters_tools_and_enforces_write_paths(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            registry = create_registry(
                workspace,
                allowed_tools={"read_file", "write_file"},
                allowed_write_paths=("README.md",),
            )

            self.assertEqual(["read_file", "write_file"], registry.names())
            registry.execute("write_file", {"path": "README.md", "content": "# Demo"})
            with self.assertRaises(PermissionError):
                registry.execute("write_file", {"path": "src/app.py", "content": "pass"})

    def test_write_file_rejects_path_outside_task_allowance(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            with self.assertRaises(PermissionError):
                write_file("other.txt", "blocked", workspace=workspace, allowed_paths=("README.md",))

            self.assertFalse((workspace / "other.txt").exists())

    def test_agent_injects_task_spec_system_message(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            repository = build_repo_map(workspace=workspace)
            spec = build_task_spec("解释项目架构", workspace, repository)
            context_plan = build_context_plan(spec, repository)
            client = RecordingClient()
            agent = CodingAgent(
                client=client,
                registry=create_registry(workspace, allowed_tools=spec.allowed_tools()),
                model="test-model",
                workspace=workspace,
                task_spec=spec,
                context_plan=context_plan,
            )

            result = agent.run("解释项目架构")

            self.assertEqual("done", result.answer)
            system_contents = [message["content"] for message in client.messages if message["role"] == "system"]
            self.assertTrue(any("Task specification:" in content for content in system_contents))
            self.assertTrue(any('"task_type": "explain_project"' in content for content in system_contents))
            self.assertTrue(any("Context plan:" in content for content in system_contents))


if __name__ == "__main__":
    unittest.main()
