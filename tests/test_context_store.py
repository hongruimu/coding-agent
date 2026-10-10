import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import build_context_plan
from agent.context_store import ContextStore
from agent.core import AgentPhase, CodingAgent
from agent.task_spec import build_task_spec
from tools import create_registry
from tools.repo import build_repo_map


RAW_MARKER = "VERY_SECRET_RAW_TOOL_CONTENT"


class CompactionClient:
    def __init__(self):
        self.read_requested = False
        self.saw_raw_result = False
        self.final_messages = []

    def create(self, *, model, messages, tools):
        phase = _current_phase(messages)
        if phase == AgentPhase.UNDERSTAND.value and not self.read_requested:
            self.read_requested = True
            function = SimpleNamespace(name="read_file", arguments=json.dumps({"path": "app.py"}))
            tool_call = SimpleNamespace(id="read-1", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        elif phase == AgentPhase.UNDERSTAND.value:
            self.saw_raw_result = RAW_MARKER in messages[-1]["content"]
            message = SimpleNamespace(content="The requested file was inspected.", tool_calls=None)
        else:
            self.final_messages = messages
            message = SimpleNamespace(content="final answer", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class ContextStoreTests(unittest.TestCase):
    def test_raw_results_are_retained_while_visible_results_are_budgeted(self):
        store = ContextStore(max_phase_tool_chars=40, max_result_chars=30)
        raw_result = "x" * 200

        visible = store.record_tool_result(
            phase="understand",
            step=1,
            tool_name="read_file",
            arguments={"path": "large.py"},
            result=raw_result,
        )

        self.assertLess(len(visible), len(raw_result))
        self.assertEqual(raw_result, store.observations[0].raw_result)
        self.assertEqual("large.py", store.evidence[0].source)

    def test_phase_artifact_contains_sources_without_raw_file_content(self):
        store = ContextStore()
        store.record_tool_result(
            phase="understand",
            step=1,
            tool_name="read_file",
            arguments={"path": "agent/core.py"},
            result="raw implementation content",
        )

        artifact = store.complete_phase(
            phase="understand",
            summary="CodingAgent controls the execution loop.",
            changed_files=(),
        )
        prompt = store.format_for_prompt()

        self.assertEqual(("agent/core.py",), artifact.files_read)
        self.assertIn("CodingAgent controls the execution loop", prompt)
        self.assertIn("agent/core.py", prompt)
        self.assertNotIn("raw implementation content", prompt)

    def test_context_prompt_has_hard_character_budget(self):
        store = ContextStore(max_prompt_chars=1_200)
        for index in range(20):
            store.record_tool_result(
                phase="understand",
                step=index,
                tool_name="read_file",
                arguments={"path": f"module_{index}.py"},
                result="content" * 200,
            )
            store.complete_phase(
                phase=f"phase-{index}",
                summary="summary" * 500,
                changed_files=(),
            )

        prompt = store.format_for_prompt()

        self.assertLessEqual(len(prompt), 1_200)
        self.assertIn("Context store:", prompt)
        json.loads(prompt.removeprefix("Context store:\n"))

    def test_agent_compacts_completed_phase_messages(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text((RAW_MARKER + "\n") * 20)
            repository = build_repo_map(workspace=workspace)
            task_spec = build_task_spec("解释 app.py", workspace, repository)
            context_plan = build_context_plan(task_spec, repository)
            client = CompactionClient()
            store = ContextStore()
            agent = CodingAgent(
                client=client,
                registry=create_registry(workspace, allowed_tools=task_spec.allowed_tools()),
                model="test-model",
                workspace=workspace,
                task_spec=task_spec,
                context_plan=context_plan,
                repo_map=repository,
                context_store=store,
            )

            result = agent.run("解释 app.py")

            final_context = "\n".join(str(message["content"]) for message in client.final_messages)
            self.assertTrue(client.saw_raw_result)
            self.assertNotIn(RAW_MARKER, final_context)
            self.assertIn("Context store:", final_context)
            self.assertIn("The requested file was inspected", final_context)
            self.assertEqual(RAW_MARKER, store.observations[0].raw_result.splitlines()[3])
            self.assertTrue(any(item.source == "app.py" for item in result.evidence))
            self.assertEqual(["understand", "finalize"], [item.phase for item in result.phase_artifacts])


def _current_phase(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] == "system" and message["content"].startswith("Current phase:"):
            return message["content"].splitlines()[0].split(":", maxsplit=1)[1].strip()
    raise AssertionError("Current phase message was not found")


if __name__ == "__main__":
    unittest.main()
