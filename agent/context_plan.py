import json
import re
from dataclasses import dataclass
from pathlib import Path

from agent.task_spec import TaskSpec, TaskType
from tools.repo import RepoMap


FORBIDDEN_PATHS = (
    ".git/",
    ".venv/",
    "venv/",
    "node_modules/",
    "__pycache__/",
    ".env",
)
SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".php", ".go", ".rs", ".java", ".kt"}
TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__"}


@dataclass(frozen=True)
class ContextPlan:
    repo_summary: str
    must_read: tuple[str, ...]
    maybe_read: tuple[str, ...]
    forbidden: tuple[str, ...]
    max_files: int
    max_chars_per_file: int

    def format_for_prompt(self) -> str:
        payload = {
            "repo_summary": self.repo_summary,
            "must_read": list(self.must_read),
            "maybe_read": list(self.maybe_read),
            "forbidden": list(self.forbidden),
            "max_files": self.max_files,
            "max_chars_per_file": self.max_chars_per_file,
        }
        return "Context plan:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_context_plan(task_spec: TaskSpec, repo_map: RepoMap) -> ContextPlan:
    files = tuple(repo_map.files)
    existing_files = set(files)
    targets = _existing(task_spec.target_files, existing_files)
    manifests = _existing(repo_map.manifests, existing_files)
    docs = _existing(repo_map.docs, existing_files)
    important_sources = tuple(
        path for path in _existing(repo_map.important_files, existing_files) if Path(path).suffix in SOURCE_SUFFIXES
    )
    source_files = tuple(path for path in files if _is_source_file(path) and not _is_test_file(path))
    test_files = tuple(path for path in files if _is_test_file(path))
    related_files = _related_files(targets, files)

    max_files = _max_files(task_spec.task_type)
    must_read: tuple[str, ...]
    maybe_read: tuple[str, ...]

    if task_spec.task_type is TaskType.CREATE_README:
        must_read = _unique(manifests, docs, important_sources, source_files[:3])
        maybe_read = _unique(test_files[:3], source_files[3:8])
    elif task_spec.task_type is TaskType.EXPLAIN_PROJECT:
        fallback_sources = () if targets else source_files[:3]
        must_read = _unique(targets, manifests, docs, important_sources, fallback_sources)
        maybe_read = _unique(related_files, source_files, test_files)
    elif task_spec.task_type is TaskType.FIX_TEST:
        related_sources = tuple(path for path in related_files if not _is_test_file(path))
        must_read = _unique(targets, related_sources[:4])
        maybe_read = _unique(manifests, related_files, test_files, source_files)
    elif task_spec.task_type is TaskType.MODIFY_CODE:
        fallback_sources = () if targets else _unique(important_sources, source_files[:3])
        must_read = _unique(targets, fallback_sources)
        maybe_read = _unique(related_files, manifests, docs, test_files, source_files)
    else:
        must_read = _unique(targets, manifests, docs)
        maybe_read = _unique(important_sources, source_files[:3], test_files[:2])

    must_read = must_read[:max_files]
    remaining = max_files - len(must_read)
    maybe_read = tuple(path for path in maybe_read if path not in must_read)[:remaining]

    return ContextPlan(
        repo_summary=_repo_summary(repo_map),
        must_read=must_read,
        maybe_read=maybe_read,
        forbidden=FORBIDDEN_PATHS,
        max_files=max_files,
        max_chars_per_file=12_000,
    )


def _repo_summary(repo_map: RepoMap) -> str:
    project_types = ", ".join(repo_map.project_types) if repo_map.project_types else "unknown"
    source_dirs = ", ".join(repo_map.source_dirs) if repo_map.source_dirs else "none"
    test_dirs = ", ".join(repo_map.test_dirs) if repo_map.test_dirs else "none"
    return (
        f"{project_types} project with {repo_map.total_files} files; "
        f"source directories: {source_dirs}; test directories: {test_dirs}."
    )


def _max_files(task_type: TaskType) -> int:
    if task_type is TaskType.CREATE_README:
        return 10
    if task_type is TaskType.UNKNOWN:
        return 8
    return 12


def _existing(paths: tuple[str, ...] | list[str], existing_files: set[str]) -> tuple[str, ...]:
    return tuple(path for path in paths if path in existing_files)


def _unique(*groups: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            if value in seen:
                continue
            seen.add(value)
            values.append(value)
    return tuple(values)


def _is_source_file(path: str) -> bool:
    return Path(path).suffix.casefold() in SOURCE_SUFFIXES


def _is_test_file(path: str) -> bool:
    file_path = Path(path)
    return any(part.casefold() in TEST_DIR_NAMES for part in file_path.parts[:-1]) or file_path.stem.casefold().startswith(
        "test_"
    )


def _related_files(targets: tuple[str, ...], files: tuple[str, ...]) -> tuple[str, ...]:
    keys = {_relation_key(path) for path in targets}
    keys.discard("")
    if not keys:
        return ()

    matches = []
    target_set = set(targets)
    for path in files:
        if path in target_set:
            continue
        relation_key = _relation_key(path)
        if relation_key in keys:
            matches.append(path)
    return tuple(matches)


def _relation_key(path: str) -> str:
    stem = Path(path).stem.casefold()
    stem = re.sub(r"^(test_|spec_)", "", stem)
    stem = re.sub(r"(_test|_spec|\.test|\.spec)$", "", stem)
    return stem if len(stem) >= 3 else ""
