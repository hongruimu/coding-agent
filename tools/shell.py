import subprocess
from pathlib import Path


MAX_OUTPUT_CHARS = 20_000


def run_command(command: str, timeout: int = 30) -> str:
    if timeout < 1 or timeout > 120:
        return "Timeout must be between 1 and 120 seconds."

    result = subprocess.run(
        command,
        shell=True,
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=timeout,
    )

    output = []
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
