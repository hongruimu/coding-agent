import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.search import find_files, grep_code, search_code, search_files


class SearchTests(unittest.TestCase):
    def test_search_files_uses_filtered_repo_map(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "agent").mkdir()
            (workspace / ".venv" / "lib").mkdir(parents=True)
            (workspace / "agent" / "core.py").write_text("class CodingAgent: pass")
            (workspace / "agent" / "workspace.py").write_text("class Workspace: pass")
            (workspace / ".venv" / "lib" / "core.py").write_text("ignored")

            result = find_files("core", workspace=workspace)
            output = search_files("*.py", workspace=workspace)

            self.assertEqual(["agent/core.py"], [match.path for match in result.matches])
            self.assertIn("- agent/core.py", output)
            self.assertIn("- agent/workspace.py", output)
            self.assertNotIn(".venv", output)

    def test_grep_code_returns_locations_and_ignores_noise(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "node_modules" / "pkg").mkdir(parents=True)
            (workspace / "src" / "main.ts").write_text("const AgentName = 'coding-agent';\n")
            (workspace / "src" / "other.ts").write_text("const value = 'CODING-AGENT';\n")
            (workspace / "node_modules" / "pkg" / "main.ts").write_text("coding-agent")

            output = grep_code("coding-agent", file_pattern="*.ts", workspace=workspace)

            self.assertIn("src/main.ts:1:", output)
            self.assertIn("src/other.ts:1:", output)
            self.assertNotIn("node_modules", output)

    def test_grep_code_has_python_fallback(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text("first line\nTarget value\n")

            with patch("tools.search.shutil.which", return_value=None):
                result = search_code("target", workspace=workspace)

            self.assertEqual("python", result.method)
            self.assertEqual("app.py", result.matches[0].path)
            self.assertEqual(2, result.matches[0].line)
            self.assertEqual(1, result.matches[0].column)

    def test_search_results_are_limited_and_marked_truncated(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            for index in range(3):
                (workspace / f"match_{index}.py").write_text("needle\n")

            files = find_files("match", max_results=2, workspace=workspace)
            with patch("tools.search.shutil.which", return_value=None):
                content = search_code("needle", max_results=2, workspace=workspace)

            self.assertEqual(2, len(files.matches))
            self.assertTrue(files.truncated)
            self.assertEqual(2, len(content.matches))
            self.assertTrue(content.truncated)

    def test_exact_result_limit_is_not_marked_truncated(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "one.py").write_text("needle\n")
            (workspace / "two.py").write_text("needle\n")

            with patch("tools.search.shutil.which", return_value=None):
                result = search_code("needle", max_results=2, workspace=workspace)

            self.assertEqual(2, len(result.matches))
            self.assertFalse(result.truncated)

    def test_python_fallback_skips_binary_files(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "binary.dat").write_bytes(b"needle\x00content")
            (workspace / "text.txt").write_text("needle")

            with patch("tools.search.shutil.which", return_value=None):
                result = search_code("needle", workspace=workspace)

            self.assertEqual(["text.txt"], [match.path for match in result.matches])

    def test_search_is_scoped_to_requested_subdirectory(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "tests").mkdir()
            (workspace / "src" / "app.py").write_text("shared text")
            (workspace / "tests" / "test_app.py").write_text("shared text")

            files = find_files("app", path="src", workspace=workspace)
            with patch("tools.search.shutil.which", return_value=None):
                content = search_code("shared", path="src", workspace=workspace)

            self.assertEqual(["app.py"], [match.path for match in files.matches])
            self.assertEqual(["app.py"], [match.path for match in content.matches])


if __name__ == "__main__":
    unittest.main()
