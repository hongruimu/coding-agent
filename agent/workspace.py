import re
from dataclasses import dataclass
from pathlib import Path


ABSOLUTE_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9._~-])/(?:[^\s`'\"<>，。；：、）)]+)")


class WorkspaceResolutionError(ValueError):
    pass


@dataclass(frozen=True)
class WorkspaceResolution:
    root: Path
    prompt_candidates: tuple[Path, ...]


def resolve_workspace(cwd: str | Path, prompt: str) -> WorkspaceResolution:
    root = Path(cwd).expanduser().resolve()
    if not root.exists():
        raise WorkspaceResolutionError(f"Workspace does not exist: {root}")

    if not root.is_dir():
        raise WorkspaceResolutionError(f"Workspace is not a directory: {root}")

    candidates = extract_workspace_candidates(prompt)
    mismatched = [candidate for candidate in candidates if not _is_within(candidate, root)]
    if mismatched:
        formatted = ", ".join(str(candidate) for candidate in mismatched)
        raise WorkspaceResolutionError(
            f"Prompt mentions path outside workspace {root}: {formatted}. "
            "Pass the intended project with --cwd to avoid operating on the wrong directory."
        )

    return WorkspaceResolution(root=root, prompt_candidates=tuple(candidates))


def extract_workspace_candidates(prompt: str) -> list[Path]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    for match in ABSOLUTE_PATH_PATTERN.finditer(prompt):
        candidate = Path(match.group(0)).expanduser()
        existing = _nearest_existing_path(candidate)
        if existing is None:
            continue

        directory = existing if existing.is_dir() else existing.parent
        resolved = directory.resolve()
        if resolved not in seen:
            seen.add(resolved)
            candidates.append(resolved)

    return candidates


def _nearest_existing_path(path: Path) -> Path | None:
    current = path
    while True:
        if current.exists():
            return current

        if current.parent == current:
            return None

        current = current.parent


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False

    return True
