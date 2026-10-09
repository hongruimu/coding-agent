import fnmatch
from pathlib import Path


MAX_FILE_CHARS = 20_000
MAX_LIST_ENTRIES = 200


def _workspace_root(workspace: str | Path) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.exists():
        raise ValueError(f"Workspace does not exist: {root}")

    if not root.is_dir():
        raise ValueError(f"Workspace is not a directory: {root}")

    return root


def _safe_path(path: str, workspace: str | Path) -> Path:
    root = _workspace_root(workspace)
    requested_path = Path(path).expanduser()
    file_path = requested_path.resolve() if requested_path.is_absolute() else (root / requested_path).resolve()

    try:
        file_path.relative_to(root)
    except ValueError:
        raise ValueError(f"Path escapes workspace: {path}") from None

    return file_path


def _display_path(path: Path, workspace: str | Path) -> str:
    return str(path.relative_to(_workspace_root(workspace)))


def _header(workspace: str | Path) -> str:
    return f"workspace: {_workspace_root(workspace)}"


def list_files(path: str = ".", *, workspace: str | Path = ".") -> str:
    directory = _safe_path(path, workspace)

    if not directory.exists():
        return f"{_header(workspace)}\nDirectory not found: {path}"

    if not directory.is_dir():
        return f"{_header(workspace)}\nNot a directory: {path}"

    entries: list[str] = []
    for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name)):
        suffix = "/" if child.is_dir() else ""
        entries.append(f"{_display_path(child, workspace)}{suffix}")

        if len(entries) >= MAX_LIST_ENTRIES:
            entries.append(f"... truncated after {MAX_LIST_ENTRIES} entries")
            break

    body = "\n".join(entries) if entries else "Directory is empty."
    return f"{_header(workspace)}\n{body}"


def read_file(path: str, *, workspace: str | Path = ".", max_chars: int = MAX_FILE_CHARS) -> str:
    if max_chars < 1:
        raise ValueError("max_chars must be greater than zero")

    file_path = _safe_path(path, workspace)

    if not file_path.exists():
        return f"{_header(workspace)}\nFile not found: {path}"

    if not file_path.is_file():
        return f"{_header(workspace)}\nNot a file: {path}"

    content = file_path.read_text(errors="replace")
    if len(content) > max_chars:
        content = content[:max_chars] + f"\n... truncated after {max_chars} characters"

    return f"{_header(workspace)}\npath: {_display_path(file_path, workspace)}\n--- content ---\n{content}"


def write_file(
    path: str,
    content: str,
    *,
    workspace: str | Path = ".",
    allowed_paths: tuple[str, ...] | None = None,
) -> str:
    file_path = _safe_path(path, workspace)
    relative_path = _display_path(file_path, workspace)
    if allowed_paths is not None and not _is_write_allowed(relative_path, allowed_paths):
        raise PermissionError(f"Write path is not allowed for this task: {relative_path}")

    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    return f"{_header(workspace)}\nWrote {len(content)} characters to {relative_path}"


def _is_write_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    if "*" in allowed_paths or "**/*" in allowed_paths:
        return True

    return any(path == pattern or fnmatch.fnmatch(path, pattern) for pattern in allowed_paths)
