import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import build_context_plan
from agent.core import CodingAgent
from agent.task_spec import build_task_spec
from agent.validation import (
    ValidationCommand,
    ValidationPlan,
    build_validation_plan,
    execute_validation_plan,
)
from tools import create_registry
from tools.repo import build_repo_map


class ValidationTests(unittest.TestCase):
    def test_python_plan_checks_syntax_and_related_tests(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "pyproject.toml").write_text("[project]\nname = 'demo'")
            (workspace / "src" / "user.py").write_text("def get_user(): return 1\n")
            (workspace / "tests" / "test_user.py").write_text("import unittest\n")
            repository = build_repo_map(workspace=workspace)

            plan = build_validation_plan(repository, ("src/user.py",))

            names = [command.name for command in plan.commands]
            self.assertIn("python syntax check", names)
            self.assertIn("python test: tests/test_user.py", names)
            syntax = next(command for command in plan.commands if command.name == "python syntax check")
            self.assertEqual(sys.executable, syntax.argv[0])
            self.assertNotIn("shell", syntax.argv)

    def test_python_plan_uses_pytest_when_project_declares_it(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "pyproject.toml").write_text("[tool.pytest.ini_options]\ntestpaths = ['tests']")
            (workspace / "src" / "user.py").write_text("def get_user(): return 1\n")
            (workspace / "tests" / "test_user.py").write_text("def test_user(): assert True\n")
            repository = build_repo_map(workspace=workspace)

            plan = build_validation_plan(repository, ("src/user.py",))

            test_command = next(command for command in plan.commands if command.name == "python related tests")
            self.assertEqual((sys.executable, "-m", "pytest", "tests/test_user.py"), test_command.argv)

    def test_markdown_plan_only_checks_git_diff(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._init_git(workspace)
            (workspace / "README.md").write_text("# Demo\n")
            repository = build_repo_map(workspace=workspace)

            plan = build_validation_plan(repository, ("README.md",))

            self.assertEqual(["git diff check"], [command.name for command in plan.commands])

    def test_node_plan_uses_existing_package_script(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "package.json").write_text(json.dumps({"scripts": {"test": "vitest", "build": "tsc"}}))
            (workspace / "src" / "app.ts").write_text("export const value = 1\n")
            repository = build_repo_map(workspace=workspace)

            plan = build_validation_plan(repository, ("src/app.ts",))

            node = next(command for command in plan.commands if command.name.startswith("node script:"))
            self.assertEqual(("npm", "run", "test"), node.argv)
            self.assertEqual(".", node.cwd)

    def test_nested_node_project_runs_validation_from_package_directory(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "frontend" / "src").mkdir(parents=True)
            (workspace / "frontend" / "package.json").write_text(json.dumps({"scripts": {"test": "vitest"}}))
            (workspace / "frontend" / "src" / "app.ts").write_text("export const value = 1\n")
            repository = build_repo_map(workspace=workspace)

            plan = build_validation_plan(repository, ("frontend/src/app.ts",))

            node = next(command for command in plan.commands if command.name.startswith("node script:"))
            self.assertEqual("frontend", node.cwd)

    def test_validation_executor_returns_structured_success_and_failure(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            plan = ValidationPlan(
                commands=(
                    ValidationCommand("success", (sys.executable, "-c", "print('ok')")),
                    ValidationCommand("failure", (sys.executable, "-c", "raise SystemExit(3)")),
                ),
                reason="test",
                required=True,
            )

            results = execute_validation_plan(plan, workspace_dir)

            self.assertTrue(results[0].passed)
            self.assertIn("ok", results[0].stdout)
            self.assertFalse(results[1].passed)
            self.assertEqual(3, results[1].exit_code)

    def test_no_changes_skips_validation(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            repository = build_repo_map(workspace=workspace_dir)

            plan = build_validation_plan(repository, ())

            self.assertFalse(plan.required)
            self.assertEqual((), plan.commands)

    def test_agent_keeps_only_latest_validation_attempt(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text("def broken(:\n")
            repository = build_repo_map(workspace=workspace)
            task_spec = build_task_spec("修改 app.py", workspace, repository)
            context_plan = build_context_plan(task_spec, repository)
            agent = CodingAgent(
                client=SimpleNamespace(),
                registry=create_registry(workspace, allowed_tools=task_spec.allowed_tools()),
                model="test-model",
                workspace=workspace,
                task_spec=task_spec,
                context_plan=context_plan,
                repo_map=repository,
            )
            agent._changed_files.append("app.py")

            agent._append_automatic_validation([])
            self.assertFalse(all(result.passed for result in agent._validation_results))

            (workspace / "app.py").write_text("VALUE = 1\n")
            agent._append_automatic_validation([])

            self.assertEqual(1, len(agent._validation_results))
            self.assertTrue(agent._validation_results[0].passed)

    def _init_git(self, workspace: Path):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
