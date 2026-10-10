import argparse
import os
import sys

from agent.context_plan import build_context_plan
from agent.core import CodingAgent
from agent.task_spec import build_task_spec
from agent.workspace import WorkspaceResolutionError, resolve_workspace
from model.openai_chat import OpenAIChatClient
from tools import create_registry
from tools.repo import build_repo_map


DEFAULT_MODEL = "gpt-4.1-mini"


def main() -> int:
    parser = argparse.ArgumentParser(description="A small Python coding agent for local project work.")
    parser.add_argument("prompt", nargs="*", help="Task for the agent. Reads stdin when omitted.")
    parser.add_argument("--cwd", default=".", help="Workspace directory to operate in.")
    parser.add_argument("--model", default=os.getenv("CODING_AGENT_MODEL", DEFAULT_MODEL))
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL"), help="Override the OpenAI API base URL.")
    parser.add_argument("--max-steps", type=int, default=12)
    args = parser.parse_args()

    prompt = " ".join(args.prompt).strip()
    if not prompt and not sys.stdin.isatty():
        prompt = sys.stdin.read().strip()

    if not prompt:
        parser.error("provide a prompt argument or pipe a prompt through stdin")

    try:
        workspace = resolve_workspace(args.cwd, prompt)
    except WorkspaceResolutionError as exc:
        parser.error(str(exc))

    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. Export it before running coding-agent.", file=sys.stderr)
        return 2

    try:
        repository = build_repo_map(workspace=workspace.root)
        task_spec = build_task_spec(prompt, workspace.root, repository)
        context_plan = build_context_plan(task_spec, repository)
        agent = CodingAgent(
            client=OpenAIChatClient(base_url=args.base_url),
            registry=create_registry(
                workspace.root,
                allowed_tools=task_spec.allowed_tools(),
                allowed_write_paths=task_spec.allowed_write_paths,
                max_read_chars=context_plan.max_chars_per_file,
            ),
            model=args.model,
            workspace=workspace.root,
            task_spec=task_spec,
            context_plan=context_plan,
            repo_map=repository,
            max_steps=args.max_steps,
        )
        result = agent.run(prompt)
    except Exception as exc:
        print(f"coding-agent failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(result.answer)
    if result.changed_files:
        print(f"\n[changed_files={', '.join(result.changed_files)}]")
    if result.validation_results:
        validation_status = "passed" if all(item.passed for item in result.validation_results) else "failed"
        print(f"\n[validation={validation_status}]")
    if result.evidence:
        print(f"\n[evidence_items={len(result.evidence)}]")
    if result.completion_report is not None:
        completion_status = "ready" if result.completion_report.ready else "incomplete"
        print(f"\n[completion={completion_status}]")
    print(f"\n[phase={result.phase.value}]")
    print(f"\n[steps_used={result.steps_used}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
