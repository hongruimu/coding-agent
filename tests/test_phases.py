import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import build_context_plan
from agent.core import AgentPhase, CodingAgent
from agent.task_spec import build_task_spec
from tools import create_registry
from tools.repo import build_repo_map


class PhaseClient:
    def __init__(self, edit: bool = False, rework_once: bool = False, read_only: bool = False):
        self.edit = edit
        self.rework_once = rework_once
        self.read_only = read_only
        self.inspected = False
        self.edit_count = 0
        self.evaluation_count = 0
        self.records: list[tuple[str, set[str]]] = []

    def create(self, *, model, messages, tools):
        phase = _current_phase(messages)
        tool_names = {tool["function"]["name"] for tool in tools}
        self.records.append((phase, tool_names))

        if phase == AgentPhase.UNDERSTAND.value and self.read_only and not self.inspected:
            self.inspected = True
            function = SimpleNamespace(name="read_file", arguments=json.dumps({"path": "app.py"}))
            tool_call = SimpleNamespace(id="inspect-1", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        should_edit = phase == AgentPhase.EXECUTE.value and self.edit and (
            self.edit_count == 0 or (self.rework_once and self.evaluation_count == 1 and self.edit_count == 1)
        )
        if should_edit:
            old_value = self.edit_count + 1
            new_value = old_value + 1
            self.edit_count += 1
            function = SimpleNamespace(
                name="replace_text",
                arguments=json.dumps(
                    {
                        "path": "app.py",
                        "old_text": f"VALUE = {old_value}",
                        "new_text": f"VALUE = {new_value}",
                    }
                ),
            )
            tool_call = SimpleNamespace(id=f"edit-{self.edit_count}", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        elif phase == AgentPhase.EVALUATE.value and self.rework_once and self.evaluation_count == 0:
            self.evaluation_count += 1
            message = SimpleNamespace(content="[NEEDS_CHANGES] adjust the implementation", tool_calls=None)
        else:
            if phase == AgentPhase.EVALUATE.value:
                self.evaluation_count += 1
                content = _completion_output(messages, "app.py")
            else:
                content = f"{phase} complete"
            message = SimpleNamespace(content=content, tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class AlwaysReadClient:
    def create(self, *, model, messages, tools):
        function = SimpleNamespace(name="repo_map", arguments="{}")
        tool_call = SimpleNamespace(id="read-1", type="function", function=function)
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class PhaseTests(unittest.TestCase):
    def test_write_task_runs_all_phases_with_phase_specific_tools(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._init_git(workspace)
            (workspace / "app.py").write_text("VALUE = 1\n")
            subprocess.run(["git", "add", "app.py"], cwd=workspace, check=True, capture_output=True)
            agent, client = self._agent(workspace, "修改 app.py", edit=True)

            result = agent.run("修改 app.py")

            phases = [phase for phase, _ in client.records]
            self.assertEqual(
                ["understand", "plan", "execute", "execute", "evaluate", "finalize"],
                phases,
            )
            understand_tools = client.records[0][1]
            plan_tools = client.records[1][1]
            execute_tools = client.records[2][1]
            evaluate_tools = client.records[-2][1]
            finalize_tools = client.records[-1][1]
            self.assertNotIn("replace_text", understand_tools)
            self.assertNotIn("run_command", understand_tools)
            self.assertNotIn("replace_text", plan_tools)
            self.assertIn("replace_text", execute_tools)
            self.assertIn("run_command", execute_tools)
            self.assertEqual({"git_diff", "read_file"}, evaluate_tools)
            self.assertEqual(set(), finalize_tools)
            self.assertEqual(("app.py",), result.changed_files)
            self.assertTrue(result.validation_results)
            self.assertTrue(all(item.passed for item in result.validation_results))
            self.assertTrue(any(item.kind == "validation" for item in result.evidence))
            self.assertTrue(any(item.kind == "diff" and item.source == "app.py" for item in result.evidence))
            self.assertIsNotNone(result.completion_report)
            self.assertTrue(result.completion_report.ready)
            self.assertEqual(AgentPhase.FINALIZE, result.phase)

    def test_read_only_task_skips_write_phases(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text("VALUE = 1\n")
            agent, client = self._agent(workspace, "解释 app.py")

            result = agent.run("解释 app.py")

            self.assertEqual(
                ["understand", "understand", "evaluate", "finalize"],
                [phase for phase, _ in client.records],
            )
            self.assertTrue(all("replace_text" not in tools for _, tools in client.records))
            self.assertIsNotNone(result.completion_report)
            self.assertTrue(result.completion_report.ready)
            self.assertEqual(AgentPhase.FINALIZE, result.phase)

    def test_evaluate_can_return_to_execute_for_rework(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            self._init_git(workspace)
            (workspace / "app.py").write_text("VALUE = 1\n")
            subprocess.run(["git", "add", "app.py"], cwd=workspace, check=True, capture_output=True)
            repository = build_repo_map(workspace=workspace)
            task_spec = build_task_spec("修改 app.py", workspace, repository)
            context_plan = build_context_plan(task_spec, repository)
            client = PhaseClient(edit=True, rework_once=True)
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

            phases = [phase for phase, _ in client.records]
            first_evaluate = phases.index("evaluate")
            self.assertIn("execute", phases[first_evaluate + 1 :])
            self.assertEqual("VALUE = 3\n", (workspace / "app.py").read_text())
            self.assertEqual(AgentPhase.FINALIZE, result.phase)

    def test_step_limit_reports_current_phase(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            repository = build_repo_map(workspace=workspace)
            task_spec = build_task_spec("解释项目", workspace, repository)
            context_plan = build_context_plan(task_spec, repository)
            agent = CodingAgent(
                client=AlwaysReadClient(),
                registry=create_registry(workspace, allowed_tools=task_spec.allowed_tools()),
                model="test-model",
                workspace=workspace,
                task_spec=task_spec,
                context_plan=context_plan,
                max_steps=1,
            )

            result = agent.run("解释项目")

            self.assertEqual(AgentPhase.UNDERSTAND, result.phase)
            self.assertIn("Step limit reached during understand phase", result.answer)

    def test_execute_tool_rejects_tool_outside_phase_policy(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text("VALUE = 1")
            agent, _ = self._agent(workspace, "修改 app.py")
            function = SimpleNamespace(
                name="replace_text",
                arguments=json.dumps({"path": "app.py", "old_text": "VALUE = 1", "new_text": "VALUE = 2"}),
            )
            tool_call = SimpleNamespace(function=function)

            output = agent._execute_tool(tool_call, allowed_tools={"read_file"})

            self.assertIn("not allowed during the current phase", output)
            self.assertEqual("VALUE = 1", (workspace / "app.py").read_text())

    def _agent(self, workspace: Path, prompt: str, edit: bool = False):
        repository = build_repo_map(workspace=workspace)
        task_spec = build_task_spec(prompt, workspace, repository)
        context_plan = build_context_plan(task_spec, repository)
        client = PhaseClient(edit=edit, read_only=not task_spec.write_allowed)
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
        return agent, client

    def _init_git(self, workspace: Path):
        if shutil.which("git") is None:
            self.skipTest("git is not available")
        subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)


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
            return "Evaluation complete.\n[COMPLETION]\n" + json.dumps({"criteria": criteria})
    raise AssertionError("Task specification message was not found")


if __name__ == "__main__":
    unittest.main()
