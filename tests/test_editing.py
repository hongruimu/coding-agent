import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import build_context_plan
from agent.core import CodingAgent
from agent.task_spec import build_task_spec
from tools import create_registry
from tools.filesystem import replace_text, write_file
from tools.git import git_diff
from tools.repo import build_repo_map


class EditingClient:
    def __init__(self):
        self.edited = False
        self.diff_result = ""

    def create(self, *, model, messages, tools):
        tool_names = {tool["function"]["name"] for tool in tools}
        if messages[-1]["role"] == "tool":
            self.diff_result = messages[-1]["content"]

        if "replace_text" in tool_names and not self.edited:
            self.edited = True
            function = SimpleNamespace(
                name="replace_text",
                arguments=json.dumps({"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}),
            )
            tool_call = SimpleNamespace(id="call-1", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        else:
            content = "updated" if not tools else "phase complete"
            message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class EditingTests(unittest.TestCase):
    def test_write_file_only_creates_new_files(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)

            write_file("new.txt", "first", workspace=workspace)
            with self.assertRaises(FileExistsError):
                write_file("new.txt", "second", workspace=workspace)

            self.assertEqual("first", (workspace / "new.txt").read_text())

    def test_replace_text_requires_one_unique_match(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            target = workspace / "app.py"
            target.write_text("VALUE = 1\n")

            replace_text("app.py", "VALUE = 1", "VALUE = 2", workspace=workspace)
            self.assertEqual("VALUE = 2\n", target.read_text())

            with self.assertRaises(ValueError):
                replace_text("app.py", "missing", "value", workspace=workspace)

            target.write_text("same\nsame\n")
            with self.assertRaises(ValueError):
                replace_text("app.py", "same", "changed", workspace=workspace)

    def test_replace_text_preserves_existing_line_endings(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            target = workspace / "app.py"
            target.write_bytes(b"VALUE = 1\r\nNEXT = 2\r\n")

            replace_text("app.py", "VALUE = 1", "VALUE = 3", workspace=workspace)

            self.assertEqual(b"VALUE = 3\r\nNEXT = 2\r\n", target.read_bytes())

    def test_replace_text_enforces_task_write_paths(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text("VALUE = 1")

            with self.assertRaises(PermissionError):
                replace_text(
                    "app.py",
                    "VALUE = 1",
                    "VALUE = 2",
                    workspace=workspace,
                    allowed_paths=("README.md",),
                )

    def test_git_diff_supports_tracked_and_untracked_files(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._init_git(workspace)
            (workspace / "tracked.txt").write_text("before\n")
            subprocess.run(["git", "add", "tracked.txt"], cwd=workspace, check=True, capture_output=True)
            (workspace / "tracked.txt").write_text("after\n")
            (workspace / "new.txt").write_text("new\n")

            tracked = git_diff("tracked.txt", workspace=workspace)
            untracked = git_diff("new.txt", workspace=workspace)

            self.assertIn("-before", tracked)
            self.assertIn("+after", tracked)
            self.assertIn("+new", untracked)

    def test_git_diff_reports_non_git_workspace(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            output = git_diff(workspace=workspace_dir)

            self.assertIn("Not a git repository", output)

    def test_agent_tracks_changed_files_and_returns_diff_after_edit(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._init_git(workspace)
            (workspace / "app.py").write_text("VALUE = 1\n")
            subprocess.run(["git", "add", "app.py"], cwd=workspace, check=True, capture_output=True)
            repository = build_repo_map(workspace=workspace)
            task_spec = build_task_spec("修改 app.py", workspace, repository)
            context_plan = build_context_plan(task_spec, repository)
            client = EditingClient()
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
            )

            result = agent.run("修改 app.py")

            self.assertEqual(("app.py",), result.changed_files)
            self.assertIn("--- git diff ---", client.diff_result)
            self.assertIn("-VALUE = 1", client.diff_result)
            self.assertIn("+VALUE = 2", client.diff_result)

    def _init_git(self, workspace: Path):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)


if __name__ == "__main__":
    unittest.main()
