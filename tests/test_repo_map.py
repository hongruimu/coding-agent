import tempfile
import subprocess
import unittest
from pathlib import Path

from tools.repo import build_repo_map, repo_map


class RepoMapTests(unittest.TestCase):
    def test_repo_map_filters_noise_and_detects_python_project(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "agent").mkdir()
            (workspace / "tests").mkdir()
            (workspace / ".venv" / "lib").mkdir(parents=True)
            (workspace / "pkg.egg-info").mkdir()
            (workspace / "__pycache__").mkdir()

            (workspace / "pyproject.toml").write_text(
                """
[project]
name = "demo"

[project.scripts]
demo = "agent.main:main"
""".strip()
            )
            (workspace / "README.md").write_text("# Demo")
            (workspace / "agent" / "main.py").write_text("def main(): pass")
            (workspace / "tests" / "test_main.py").write_text("def test_main(): pass")
            (workspace / ".venv" / "lib" / "ignored.py").write_text("ignored")
            (workspace / "pkg.egg-info" / "PKG-INFO").write_text("ignored")
            (workspace / "__pycache__" / "ignored.pyc").write_text("ignored")

            output = repo_map(workspace=workspace)

            self.assertIn("project_types: python", output)
            self.assertIn("- pyproject.toml", output)
            self.assertIn("- README.md", output)
            self.assertIn("- agent/", output)
            self.assertIn("- tests/", output)
            self.assertIn("- python script demo = agent.main:main", output)
            self.assertNotIn("ignored.py", output)
            self.assertNotIn("PKG-INFO", output)
            self.assertNotIn("ignored.pyc", output)

    def test_repo_map_detects_php_typescript_and_javascript_projects(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "public").mkdir()

            (workspace / "composer.json").write_text(
                '{"scripts":{"test":"phpunit"}}'
            )
            (workspace / "package.json").write_text(
                '{"scripts":{"build":"tsc","test":"vitest"}}'
            )
            (workspace / "tsconfig.json").write_text("{}")
            (workspace / "src" / "index.ts").write_text("export const answer = 42")
            (workspace / "public" / "app.js").write_text("console.log('demo')")
            (workspace / "src" / "Controller.php").write_text("<?php echo 'demo';")

            output = repo_map(workspace=workspace)

            self.assertIn("project_types: node, typescript, javascript, php", output)
            self.assertIn("- composer.json", output)
            self.assertIn("- package.json", output)
            self.assertIn("- tsconfig.json", output)
            self.assertIn("- src/", output)
            self.assertIn("- public/", output)
            self.assertIn("- npm script build = tsc", output)
            self.assertIn("- composer script test = phpunit", output)

    def test_repo_map_path_is_relative_to_requested_subdirectory_in_git_repo(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "src").mkdir()
            (workspace / "README.md").write_text("# Demo")
            (workspace / "src" / "app.py").write_text("print('demo')")

            try:
                subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
            except (OSError, subprocess.CalledProcessError):
                self.skipTest("git is not available")

            output = repo_map(path="src", workspace=workspace)

            self.assertIn(f"workspace: {workspace.resolve()}", output)
            self.assertIn(f"analyzed_path: {(workspace / 'src').resolve()}", output)
            self.assertIn("discovery_method: git", output)
            self.assertIn("- app.py", output)
            self.assertNotIn("- src/app.py", output)
            self.assertNotIn("- README.md", output)

    def test_build_repo_map_returns_structured_data(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "pyproject.toml").write_text("[project]\nname = 'demo'")
            (workspace / "README.md").write_text("# Demo")

            result = build_repo_map(workspace=workspace)

            self.assertEqual(workspace.resolve(), result.workspace)
            self.assertEqual(["python"], result.project_types)
            self.assertEqual(["pyproject.toml"], result.manifests)
            self.assertEqual(["README.md"], result.docs)


if __name__ == "__main__":
    unittest.main()
