import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.context_plan import build_context_plan
from agent.core import AgentPhase, CodingAgent
from agent.events import JsonlEventLogger, RunTrace, summarize_tool_arguments
from agent.task_spec import build_task_spec
from tools import create_registry
from tools.repo import build_repo_map


RAW_MARKER = "PRIVATE_FILE_CONTENT_SHOULD_NOT_BE_LOGGED"


class RecordingLogger:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class ReadTraceClient:
    def __init__(self):
        self.read_requested = False

    def create(self, *, model, messages, tools):
        phase = _current_phase(messages)
        if phase == AgentPhase.UNDERSTAND.value and not self.read_requested:
            self.read_requested = True
            function = SimpleNamespace(name="read_file", arguments=json.dumps({"path": "app.py"}))
            tool_call = SimpleNamespace(id="read-1", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        elif phase == AgentPhase.EVALUATE.value:
            message = SimpleNamespace(content=_read_completion(messages, "app.py"), tool_calls=None)
        else:
            message = SimpleNamespace(content=f"{phase} complete", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class WriteTraceClient:
    def __init__(self):
        self.edited = False

    def create(self, *, model, messages, tools):
        phase = _current_phase(messages)
        if phase == AgentPhase.PLAN.value:
            message = SimpleNamespace(content=_write_plan(messages), tool_calls=None)
        elif phase == AgentPhase.EXECUTE.value and not self.edited:
            self.edited = True
            function = SimpleNamespace(
                name="replace_text",
                arguments=json.dumps(
                    {
                        "path": "app.py",
                        "old_text": "VALUE = 1",
                        "new_text": "VALUE = 2",
                    }
                ),
            )
            tool_call = SimpleNamespace(id="edit-1", type="function", function=function)
            message = SimpleNamespace(content=None, tool_calls=[tool_call])
        elif phase == AgentPhase.EVALUATE.value:
            message = SimpleNamespace(content=_write_completion(messages, "app.py"), tool_calls=None)
        else:
            message = SimpleNamespace(content=f"{phase} complete", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FailingClient:
    def create(self, *, model, messages, tools):
        raise RuntimeError("OPENAI_API_KEY=secret-value sk-abcdefghijk")


class AlwaysToolClient:
    def create(self, *, model, messages, tools):
        function = SimpleNamespace(name="repo_map", arguments="{}")
        tool_call = SimpleNamespace(id="tool-1", type="function", function=function)
        message = SimpleNamespace(content=None, tool_calls=[tool_call])
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class EventTests(unittest.TestCase):
    def test_jsonl_logger_writes_sanitized_events(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = RunTrace(JsonlEventLogger(path), run_id="run-1")

            trace.emit(
                "sample",
                data={
                    "content": "private file body",
                    "error": "OPENAI_API_KEY=secret-value sk-abcdefghijk",
                    "safe": "x" * 600,
                },
            )

            payload = json.loads(path.read_text().strip())
            serialized = json.dumps(payload)
            self.assertEqual("run-1", payload["run_id"])
            self.assertEqual(1, payload["schema_version"])
            self.assertEqual(1, payload["sequence"])
            self.assertEqual("sample", payload["type"])
            self.assertNotIn("private file body", serialized)
            self.assertNotIn("secret-value", serialized)
            self.assertNotIn("sk-abcdefghijk", serialized)
            self.assertIn("redacted", serialized)

    def test_tool_argument_summary_omits_edit_content_and_command_arguments(self):
        edit_summary = summarize_tool_arguments(
            "replace_text",
            {"path": "app.py", "old_text": "secret old", "new_text": "secret new"},
        )
        command_summary = summarize_tool_arguments(
            "run_command",
            {"command": "OPENAI_API_KEY=secret python -m unittest", "timeout": 30},
        )

        self.assertEqual("app.py", edit_summary["path"])
        self.assertNotIn("old_text", edit_summary)
        self.assertNotIn("new_text", edit_summary)
        self.assertEqual(len("secret old"), edit_summary["old_text_chars"])
        self.assertEqual("python", command_summary["command_name"])
        self.assertNotIn("secret", json.dumps(command_summary))

    def test_read_only_run_records_phase_tool_completion_and_no_raw_content(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text(RAW_MARKER)
            logger = RecordingLogger()
            agent = _agent(workspace, "解释 app.py", ReadTraceClient(), logger)

            result = agent.run("解释 app.py")

            event_types = [event.type for event in logger.events]
            self.assertEqual("task_started", event_types[0])
            self.assertEqual("task_finished", event_types[-1])
            self.assertIn("tool_called", event_types)
            self.assertIn("tool_finished", event_types)
            self.assertIn("completion_checked", event_types)
            self.assertEqual(result.run_id, logger.events[0].run_id)
            self.assertEqual(list(range(1, len(logger.events) + 1)), [event.sequence for event in logger.events])
            self.assertEqual("success", logger.events[-1].data["status"])
            self.assertNotIn(RAW_MARKER, json.dumps([event.as_dict() for event in logger.events]))

    def test_write_run_records_plan_change_validation_and_work_items(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text("VALUE = 1\n")
            logger = RecordingLogger()
            agent = _agent(workspace, "修改 app.py", WriteTraceClient(), logger)

            result = agent.run("修改 app.py")

            event_types = [event.type for event in logger.events]
            self.assertIn("plan_created", event_types)
            self.assertIn("file_changed", event_types)
            self.assertIn("validation_started", event_types)
            self.assertIn("validation_finished", event_types)
            self.assertIn("work_items_updated", event_types)
            tool_event = next(event for event in logger.events if event.type == "tool_called")
            serialized = json.dumps(tool_event.as_dict())
            self.assertNotIn("VALUE = 1", serialized)
            self.assertNotIn("VALUE = 2", serialized)
            self.assertTrue(result.completion_report.ready)

    def test_exception_records_error_task_finished_event(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            (workspace / "app.py").write_text("VALUE = 1\n")
            logger = RecordingLogger()
            agent = _agent(workspace, "解释 app.py", FailingClient(), logger)

            with self.assertRaises(RuntimeError):
                agent.run("解释 app.py")

            last_event = logger.events[-1]
            self.assertEqual("task_finished", last_event.type)
            self.assertEqual("error", last_event.data["status"])
            self.assertNotIn("secret-value", json.dumps(last_event.as_dict()))

    def test_step_limit_records_task_finished_event(self):
        with tempfile.TemporaryDirectory() as workspace_dir:
            workspace = Path(workspace_dir)
            logger = RecordingLogger()
            agent = _agent(workspace, "解释项目", AlwaysToolClient(), logger, max_steps=1)

            result = agent.run("解释项目")

            self.assertEqual("step_limit", logger.events[-1].data["status"])
            self.assertEqual(1, result.steps_used)


def _agent(workspace: Path, prompt: str, client, logger, *, max_steps: int = 12) -> CodingAgent:
    repository = build_repo_map(workspace=workspace)
    task_spec = build_task_spec(prompt, workspace, repository)
    context_plan = build_context_plan(task_spec, repository)
    return CodingAgent(
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
        event_logger=logger,
        max_steps=max_steps,
    )


def _current_phase(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message["role"] == "system" and message["content"].startswith("Current phase:"):
            return message["content"].splitlines()[0].split(":", maxsplit=1)[1].strip()
    raise AssertionError("Current phase message was not found")


def _write_plan(messages: list[dict]) -> str:
    task_spec = _task_spec(messages)
    step = {
        "id": "step-1",
        "objective": "Modify app.py.",
        "target_files": ["app.py"],
        "acceptance_criteria": list(range(len(task_spec["acceptance_criteria"]))),
        "validation_hint": "Run automatic validation.",
    }
    return "[PLAN]\n" + json.dumps({"steps": [step]})


def _read_completion(messages: list[dict], source: str) -> str:
    task_spec = _task_spec(messages)
    criteria = [
        {"index": index, "satisfied": True, "evidence": [source]}
        for index, _ in enumerate(task_spec["acceptance_criteria"])
    ]
    return "[COMPLETION]\n" + json.dumps({"criteria": criteria})


def _write_completion(messages: list[dict], source: str) -> str:
    task_spec = _task_spec(messages)
    criteria = [
        {"index": index, "satisfied": True, "evidence": [source]}
        for index, _ in enumerate(task_spec["acceptance_criteria"])
    ]
    payload = {
        "work_items": [{"id": "step-1", "satisfied": True, "evidence": [source]}],
        "criteria": criteria,
    }
    return "[COMPLETION]\n" + json.dumps(payload)


def _task_spec(messages: list[dict]) -> dict:
    for message in messages:
        content = message.get("content")
        if message.get("role") == "system" and isinstance(content, str) and content.startswith("Task specification:"):
            return json.loads(content.removeprefix("Task specification:\n"))
    raise AssertionError("Task specification message was not found")


if __name__ == "__main__":
    unittest.main()
