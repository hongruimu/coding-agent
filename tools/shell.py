import subprocess
from pathlib import Path


MAX_OUTPUT_CHARS = 20_000


def _workspace_root(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"Workspace does not exist: {root}")

    if not root.is_dir():
        raise ValueError(f"Workspace is not a directory: {root}")

    return root


def run_command(command: str, timeout: int = 30, *, workspace: str | Path = ".") -> str:
    if timeout < 1 or timeout > 120:
        return "Timeout must be between 1 and 120 seconds."

    root = _workspace_root(workspace)

    result = subprocess.run(
        command,
        shell=True,
        cwd=root,
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    output = []
    output.append(f"workspace: {root}")
    output.append(f"exit_code={result.returncode}")

    if result.stdout:
        output.append("stdout:")
        output.append(result.stdout)

    if result.stderr:
        output.append("stderr:")
        output.append(result.stderr)

    text = "\n".join(output)
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n... truncated after {MAX_OUTPUT_CHARS} characters"

    return text
