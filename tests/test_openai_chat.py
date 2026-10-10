import unittest
from types import SimpleNamespace

from model.openai_chat import OpenAIChatClient


class CompletionRecorder:
    def __init__(self):
        self.arguments = None

    def create(self, **arguments):
        self.arguments = arguments
        return "response"


class OpenAIChatClientTests(unittest.TestCase):
    def test_empty_tools_are_omitted_from_request(self):
        recorder = CompletionRecorder()
        client = object.__new__(OpenAIChatClient)
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))

        response = client.create(model="test-model", messages=[{"role": "user", "content": "hello"}], tools=[])

        self.assertEqual("response", response)
        self.assertNotIn("tools", recorder.arguments)
        self.assertNotIn("tool_choice", recorder.arguments)

    def test_non_empty_tools_enable_automatic_tool_choice(self):
        recorder = CompletionRecorder()
        client = object.__new__(OpenAIChatClient)
        client.client = SimpleNamespace(chat=SimpleNamespace(completions=recorder))
        tools = [{"type": "function", "function": {"name": "read_file"}}]

        client.create(model="test-model", messages=[], tools=tools)

        self.assertEqual(tools, recorder.arguments["tools"])
        self.assertEqual("auto", recorder.arguments["tool_choice"])


if __name__ == "__main__":
    unittest.main()
