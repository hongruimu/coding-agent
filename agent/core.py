import json
from dataclasses import dataclass
from typing import Any, Protocol

from tools.registry import ToolRegistry


SYSTEM_PROMPT = """You are coding-agent, a practical Python coding assistant.

Your goal is not to imitate Codex internals. Your goal is to help the user finish real coding work through this loop:
1. Understand the request and inspect the workspace before changing files.
2. Make a short plan for non-trivial tasks.
3. Execute focused changes with available tools.
4. Validate with the most relevant command when possible.
5. Report what changed, validation results, and any remaining risk.

Rules:
- Prefer small, reversible steps.
- Read files before editing them.
- Keep changes minimal and directly related to the user request.
- Avoid destructive commands such as rm, git reset, and force pushes unless the user explicitly asks.
- Do not invent command results; run commands when validation matters.
"""


@dataclass
class AgentResult:
    answer: str
    steps_used: int


class ChatClient(Protocol):
    def create(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        pass


class CodingAgent:
    def __init__(self, client: ChatClient, registry: ToolRegistry, model: str, max_steps: int = 12):
        self.client = client
        self.registry = registry
        self.model = model
        self.max_steps = max_steps

    def run(self, user_prompt: str) -> AgentResult:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        final_answer = ""
        for step in range(1, self.max_steps + 1):
            response = self.client.create(
                model=self.model,
                messages=messages,
                tools=self.registry.definitions(),
            )
            message = response.choices[0].message
            tool_calls = message.tool_calls or []

            messages.append(self._assistant_message(message))

            if message.content:
                final_answer = message.content

            if not tool_calls:
                print(f"======={step}=======")
                print(messages)
                print(f">>>>======={step}===")
                return AgentResult(answer=final_answer, steps_used=step)

            for tool_call in tool_calls:
                result = self._execute_tool(tool_call)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result,
                    }
                )
            print(f"=======ENDFOR=======")
            print(messages)
            print(f">>>>=======ENDFOR===")

        messages.append(
            {
                "role": "user",
                "content": "You reached the step limit. Summarize progress, files changed, validation, and next action.",
            }
        )
        print("=======>END=======>")
        print(messages)
        print("=======>END=======>")
        response = self.client.create(
            model=self.model,
            messages=messages,
            tools=self.registry.definitions(),
        )
        return AgentResult(answer=response.choices[0].message.content or final_answer, steps_used=self.max_steps)

    def _assistant_message(self, message: Any) -> dict[str, Any]:
        assistant_message: dict[str, Any] = {
            "role": "assistant",
            "content": message.content,
        }

        if message.tool_calls:
            assistant_message["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": tool_call.type,
                    "function": {
                        "name": tool_call.function.name,
                        "arguments": tool_call.function.arguments,
                    },
                }
                for tool_call in message.tool_calls
            ]

        return assistant_message

    def _execute_tool(self, tool_call: Any) -> str:
        name = tool_call.function.name
        try:
            arguments = json.loads(tool_call.function.arguments or "{}")
            result = self.registry.execute(name, arguments)
        except Exception as exc:
            return f"Tool {name} failed: {type(exc).__name__}: {exc}"

        if isinstance(result, str):
            return result

        return json.dumps(result, ensure_ascii=False)
