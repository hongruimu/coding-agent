from pathlib import Path


MAX_FILE_CHARS = 20_000
MAX_LIST_ENTRIES = 200


def _workspace_root() -> Path:
    return Path.cwd().resolve()


def _safe_path(path: str) -> Path:
    root = _workspace_root()
    file_path = (root / path).resolve()

    try:
        file_path.relative_to(root)
    except ValueError:
        raise ValueError(f"Path escapes workspace: {path}") from None

    return file_path


def _display_path(path: Path) -> str:
    return str(path.relative_to(_workspace_root()))


def list_files(path: str = ".") -> str:
    directory = _safe_path(path)

    if not directory.exists():
        return f"Directory not found: {path}"

    if not directory.is_dir():
        return f"Not a directory: {path}"

    entries: list[str] = []
    for child in sorted(directory.iterdir(), key=lambda item: (not item.is_dir(), item.name)):
        suffix = "/" if child.is_dir() else ""
        entries.append(f"{_display_path(child)}{suffix}")

        if len(entries) >= MAX_LIST_ENTRIES:
            entries.append(f"... truncated after {MAX_LIST_ENTRIES} entries")
            break

    return "\n".join(entries) if entries else "Directory is empty."


def read_file(path: str) -> str:
    file_path = _safe_path(path)

    if not file_path.exists():
        return f"File not found: {path}"

    if not file_path.is_file():
        return f"Not a file: {path}"

    content = file_path.read_text(errors="replace")
    if len(content) > MAX_FILE_CHARS:
        return content[:MAX_FILE_CHARS] + f"\n... truncated after {MAX_FILE_CHARS} characters"

    return content


def write_file(path: str, content: str) -> str:
    file_path = _safe_path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content)
    return f"Wrote {len(content)} characters to {_display_path(file_path)}"
