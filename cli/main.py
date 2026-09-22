import argparse
import os
import sys
from pathlib import Path

from agent.core import CodingAgent
from model.openai_chat import OpenAIChatClient
from tools import create_registry
import agent


DEFAULT_MODEL = "gpt-4.1-mini"


def main() -> int:
    parser = argparse.ArgumentParser(description="A small Python coding agent for local project work.")
    parser.add_argument("prompt", nargs="*", help="Task for the agent. Reads stdin when omitted.")
    parser.add_argument("--cwd", default=".", help="Workspace directory to operate in.")
    parser.add_argument("--model", default=os.getenv("CODING_AGENT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-steps", type=int, default=12)
    args = parser.parse_args()

    prompt = " ".join(args.prompt).strip()
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()

    if not prompt:
        parser.error("provide a prompt argument or pipe a prompt through stdin")

    workspace = Path(args.cwd).resolve()
    if not workspace.exists() or not workspace.is_dir():
        parser.error(f"workspace does not exist or is not a directory: {args.cwd}")

    os.chdir(workspace)

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. Export it before running coding-agent.", file=sys.stderr)
        return 2

    try:
        agent = CodingAgent(
            client=OpenAIChatClient(),
            registry=create_registry(),
            model=args.model,
            max_steps=args.max_steps,
        )
        result = agent.run(prompt)
    except Exception as exc:
        print(f"coding-agent failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(result.answer)
    print(f"\n[steps_used={result.steps_used}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
