import os
import tempfile
import unittest
from pathlib import Path

from agent.workspace import WorkspaceResolutionError, resolve_workspace
from tools import create_registry


class WorkspaceTests(unittest.TestCase):
    def test_registry_tools_use_bound_workspace_not_process_cwd(self):
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as other_dir:
            workspace = Path(workspace_dir)
            (workspace / "project_file.txt").write_text("hello")
            (Path(other_dir) / "other_file.txt").write_text("wrong")

            original_cwd = Path.cwd()
            try:
                os.chdir(other_dir)
                registry = create_registry(workspace)

                output = registry.execute("list_files", {"path": "."})
            finally:
                os.chdir(original_cwd)

            self.assertIn(f"workspace: {workspace.resolve()}", output)
            self.assertIn("project_file.txt", output)
            self.assertNotIn("other_file.txt", output)

    def test_resolve_workspace_rejects_prompt_path_outside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace_dir, tempfile.TemporaryDirectory() as mentioned_dir:
            prompt = f"为{mentioned_dir} 这个项目建一个 README.md 文件"

            with self.assertRaises(WorkspaceResolutionError):
                resolve_workspace(workspace_dir, prompt)

    def test_resolve_workspace_accepts_prompt_path_inside_workspace(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            child = Path(workspace_dir) / "child"
            child.mkdir()
            prompt = f"分析 {child} 下面的代码"

            workspace = resolve_workspace(workspace_dir, prompt)

            self.assertEqual(Path(workspace_dir).resolve(), workspace.root)
            self.assertEqual((child.resolve(),), workspace.prompt_candidates)


if __name__ == "__main__":
    unittest.main()
