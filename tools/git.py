import os
import subprocess
from pathlib import Path


MAX_DIFF_CHARS = 20_000
GIT_TIMEOUT_SECONDS = 10


def git_diff(path: str | None = None, *, workspace: str | Path = ".") -> str:
    workspace_root = _workspace_root(workspace)
    git_root = _git_root(workspace_root)
    if git_root is None:
        return f"workspace: {workspace_root}\nNot a git repository."

    pathspec = _pathspec(path, workspace_root, git_root)
    result = _run_git(["diff", "--no-ext-diff", "--", pathspec], git_root)
    if result.returncode != 0:
        return _format_error(workspace_root, result.stderr)

    diff = result.stdout
    if path is not None:
        file_path = _safe_path(path, workspace_root)
        if not diff and file_path.is_file() and _is_untracked(pathspec, git_root):
            diff = _untracked_diff(file_path, git_root)
    else:
        untracked = _untracked_files(pathspec, git_root)
        if untracked:
            suffix = "\nUntracked files not included in the diff:\n" + "\n".join(f"- {item}" for item in untracked)
            diff = diff.rstrip() + suffix

    if not diff.strip():
        label = path or "."
        return f"workspace: {workspace_root}\nNo diff for {label}."

    return _truncate(f"workspace: {workspace_root}\n--- git diff ---\n{diff.rstrip()}")


def _workspace_root(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"Workspace does not exist: {root}")
    if not root.is_dir():
        raise ValueError(f"Workspace is not a directory: {root}")
    return root


def _safe_path(path: str, workspace: Path) -> Path:
    requested = Path(path).expanduser()
    resolved = requested.resolve() if requested.is_absolute() else (workspace / requested).resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError:
        raise ValueError(f"Path escapes workspace: {path}") from None
    return resolved


def _git_root(workspace: Path) -> Path | None:
    result = _run_git(["rev-parse", "--show-toplevel"], workspace)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip()).resolve()


def _pathspec(path: str | None, workspace: Path, git_root: Path) -> str:
    target = _safe_path(path, workspace) if path is not None else workspace
    return target.relative_to(git_root).as_posix() or "."


def _is_untracked(pathspec: str, git_root: Path) -> bool:
    result = _run_git(["ls-files", "--error-unmatch", "--", pathspec], git_root)
    return result.returncode != 0


def _untracked_files(pathspec: str, git_root: Path) -> list[str]:
    result = _run_git(["ls-files", "--others", "--exclude-standard", "--", pathspec], git_root)
    if result.returncode != 0:
        return []
    return result.stdout.splitlines()[:50]


def _untracked_diff(file_path: Path, git_root: Path) -> str:
    result = _run_git(["diff", "--no-index", "--", os.devnull, str(file_path)], git_root)
    if result.returncode not in {0, 1}:
        return ""
    return result.stdout


def _run_git(arguments: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=cwd,
            text=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(["git", *arguments], 1, "", str(exc))


def _format_error(workspace: Path, stderr: str) -> str:
    detail = stderr.strip() or "git diff failed"
    return f"workspace: {workspace}\n{detail}"


def _truncate(text: str) -> str:
    if len(text) <= MAX_DIFF_CHARS:
        return text
    return text[:MAX_DIFF_CHARS] + f"\n... truncated after {MAX_DIFF_CHARS} characters"
