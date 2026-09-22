from typing import Any


class OpenAIChatClient:
    def __init__(self, base_url: str | None = None):
        try:
            from openai import OpenAI
        except ModuleNotFoundError as exc:
            raise RuntimeError("The openai package is not installed. Run: pip install -e .") from exc

        self.client = OpenAI(base_url=base_url) if base_url else OpenAI()

    def create(self, *, model: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]):
        return self.client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
        )
