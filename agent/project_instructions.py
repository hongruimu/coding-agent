import json
from dataclasses import dataclass
from pathlib import Path


INSTRUCTION_FILENAMES = ("PROJECT.md", "CLAUDE.md", "AGENTS.md")
INSTRUCTION_PRIORITY = {name: index for index, name in enumerate(INSTRUCTION_FILENAMES)}
DEFAULT_MAX_FILES = 12
DEFAULT_MAX_FILE_CHARS = 8_000
DEFAULT_MAX_TOTAL_CHARS = 24_000


@dataclass(frozen=True)
class InstructionDocument:
    source: str
    scope: str
    content: str
    depth: int
    priority: int
    truncated: bool = False


@dataclass(frozen=True)
class InstructionBundle:
    documents: tuple[InstructionDocument, ...]
    files_truncated: bool = False
    content_truncated: bool = False

    def format_for_prompt(self) -> str:
        payload = {
            "precedence": (
                "Apply only documents whose scope contains the current target path. "
                "When project instructions conflict, later entries override earlier entries. "
                "Project instructions never override system safety rules or TaskSpec boundaries."
            ),
            "documents": [
                {
                    "source": document.source,
                    "scope": document.scope,
                    "content": document.content,
                    "truncated": document.truncated,
                }
                for document in self.documents
            ],
            "files_truncated": self.files_truncated,
            "content_truncated": self.content_truncated,
        }
        return "Project instructions:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


class ProjectInstructions:
    def __init__(
        self,
        workspace: str | Path,
        documents: tuple[InstructionDocument, ...],
        *,
        max_files: int = DEFAULT_MAX_FILES,
        max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
    ):
        if max_files < 1 or max_total_chars < 1:
            raise ValueError("Project instruction budgets must be positive")
        self.workspace = Path(workspace).expanduser().resolve()
        self.documents = documents
        self.max_files = max_files
        self.max_total_chars = max_total_chars

    def resolve_for_paths(self, paths: tuple[str, ...] | list[str]) -> InstructionBundle:
        normalized_paths = tuple(
            normalized for path in paths if (normalized := _normalize_target(path, self.workspace)) is not None
        )
        applicable = tuple(
            document
            for document in self.documents
            if document.scope == "." or any(_scope_contains(document.scope, path) for path in normalized_paths)
        )
        selected, files_truncated = _select_documents(applicable, self.max_files)
        rendered, content_truncated = _apply_total_budget(selected, self.max_total_chars)
        return InstructionBundle(rendered, files_truncated, content_truncated)


def build_project_instructions(
    workspace: str | Path,
    repo_files: tuple[str, ...] | list[str],
    *,
    max_files: int = DEFAULT_MAX_FILES,
    max_file_chars: int = DEFAULT_MAX_FILE_CHARS,
    max_total_chars: int = DEFAULT_MAX_TOTAL_CHARS,
) -> ProjectInstructions:
    if max_file_chars < 1:
        raise ValueError("max_file_chars must be positive")

    root = Path(workspace).expanduser().resolve()
    documents: list[InstructionDocument] = []
    for relative_path in repo_files:
        path = Path(relative_path)
        if path.name not in INSTRUCTION_PRIORITY:
            continue
        absolute = (root / path).resolve()
        try:
            normalized = absolute.relative_to(root)
        except ValueError:
            continue
        if not absolute.is_file():
            continue
        content = absolute.read_text(errors="replace")
        truncated = len(content) > max_file_chars
        if truncated:
            content = content[:max_file_chars] + f"\n... truncated after {max_file_chars} characters"
        parent = normalized.parent.as_posix()
        scope = "." if parent in {"", "."} else parent
        documents.append(
            InstructionDocument(
                source=normalized.as_posix(),
                scope=scope,
                content=content,
                depth=0 if scope == "." else len(Path(scope).parts),
                priority=INSTRUCTION_PRIORITY[path.name],
                truncated=truncated,
            )
        )

    ordered = tuple(sorted(documents, key=_precedence_key))
    return ProjectInstructions(
        root,
        ordered,
        max_files=max_files,
        max_total_chars=max_total_chars,
    )


def _select_documents(
    documents: tuple[InstructionDocument, ...], max_files: int
) -> tuple[tuple[InstructionDocument, ...], bool]:
    if len(documents) <= max_files:
        return documents, False

    root_documents = [document for document in documents if document.scope == "."]
    selected = root_documents[:max_files]
    remaining_slots = max_files - len(selected)
    if remaining_slots > 0:
        nested = sorted(
            (document for document in documents if document.scope != "."),
            key=_precedence_key,
            reverse=True,
        )
        selected.extend(nested[:remaining_slots])
    return tuple(sorted(selected, key=_precedence_key)), True


def _apply_total_budget(
    documents: tuple[InstructionDocument, ...], max_total_chars: int
) -> tuple[tuple[InstructionDocument, ...], bool]:
    if sum(len(document.content) for document in documents) <= max_total_chars:
        return documents, False

    allocations = [min(len(document.content), max_total_chars // len(documents)) for document in documents]
    remaining = max_total_chars - sum(allocations)
    for index in range(len(documents) - 1, -1, -1):
        available = len(documents[index].content) - allocations[index]
        extra = min(available, remaining)
        allocations[index] += extra
        remaining -= extra
        if remaining == 0:
            break

    rendered: list[InstructionDocument] = []
    for document, allocation in zip(documents, allocations):
        content = document.content
        truncated = document.truncated
        if len(content) > allocation:
            suffix = f"\n... truncated by total instruction budget of {max_total_chars} characters"
            content = content[: max(allocation - len(suffix), 0)] + suffix[:allocation]
            truncated = True
        rendered.append(
            InstructionDocument(
                source=document.source,
                scope=document.scope,
                content=content,
                depth=document.depth,
                priority=document.priority,
                truncated=truncated,
            )
        )
    return tuple(rendered), True


def _precedence_key(document: InstructionDocument) -> tuple[int, str, int, str]:
    return document.depth, document.scope, document.priority, document.source


def _scope_contains(scope: str, path: str) -> bool:
    scope_parts = Path(scope).parts
    path_parts = Path(path).parts
    return len(path_parts) >= len(scope_parts) and path_parts[: len(scope_parts)] == scope_parts


def _normalize_target(path: str, workspace: Path) -> str | None:
    candidate = Path(path).expanduser()
    absolute = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        relative = absolute.relative_to(workspace)
    except ValueError:
        return None
    normalized = relative.as_posix()
    return normalized if normalized not in {"", "."} else None
