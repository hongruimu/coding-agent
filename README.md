# coding-agent

A small Python coding agent for real project work. The target is not a line-by-line Codex clone. The target is a useful loop:

1. understand the request
2. inspect the workspace
3. plan focused changes
4. execute with tools
5. validate and summarize

## Current milestone

This first milestone provides a runnable CLI with a tool-calling loop:

- `list_files` lists workspace files.
- `read_file` reads bounded file content.
- `write_file` writes complete files inside the workspace.
- `run_command` runs validation commands inside the workspace.

## Usage

```bash
export OPENAI_API_KEY=...
coding-agent --cwd /path/to/project "Add a README usage section"
```

You can choose a model with either `--model` or `CODING_AGENT_MODEL`:

```bash
CODING_AGENT_MODEL=gpt-4.1-mini coding-agent "Inspect this repo and suggest the next change"
```

You can override the API endpoint with either `--base-url` or `OPENAI_BASE_URL`:

```bash
OPENAI_BASE_URL=https://api.example.com/v1 coding-agent "Inspect this repo"
coding-agent --base-url https://api.example.com/v1 "Inspect this repo"
```

## Next milestones

- Add patch-based editing instead of whole-file writes.
- Add visible progress events for each tool call.
- Add approval rules for risky shell commands.
- Add persistent task memory and resumable sessions.
- Add focused tests around tool safety and agent message formatting.
