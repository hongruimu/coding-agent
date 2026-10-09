import fnmatch
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from tools.repo import IGNORED_NAMES, IGNORED_PATTERNS, RepoMap, build_repo_map


DEFAULT_MAX_RESULTS = 50
MAX_RESULTS = 200
MAX_PREVIEW_CHARS = 240
MAX_FALLBACK_FILE_BYTES = 2_000_000
SEARCH_TIMEOUT_SECONDS = 15


@dataclass(frozen=True)
class SearchMatch:
    path: str
    line: int | None = None
    column: int | None = None
    preview: str = ""


@dataclass(frozen=True)
class SearchResult:
    query: str
    root: Path
    method: str
    matches: list[SearchMatch]
    truncated: bool

    def format_for_llm(self) -> str:
        lines = [
            f"query: {self.query}",
            f"search_root: {self.root}",
            f"method: {self.method}",
            f"matches: {len(self.matches)}",
        ]

        for match in self.matches:
            location = match.path
            if match.line is not None:
                location += f":{match.line}"
            if match.column is not None:
                location += f":{match.column}"

            lines.append(f"- {location}")
            if match.preview:
                lines.append(f"  {match.preview}")

        if not self.matches:
            lines.append("- none")

        if self.truncated:
            lines.append("results_truncated: true")

        return "\n".join(lines)


def search_files(
    pattern: str,
    path: str = ".",
    max_results: int = DEFAULT_MAX_RESULTS,
    *,
    workspace: str | Path = ".",
) -> str:
    result = find_files(pattern, path=path, max_results=max_results, workspace=workspace)
    return result.format_for_llm()


def find_files(
    pattern: str,
    path: str = ".",
    max_results: int = DEFAULT_MAX_RESULTS,
    *,
    workspace: str | Path = ".",
) -> SearchResult:
    query = _validate_query(pattern)
    limit = _validate_max_results(max_results)
    repo = build_repo_map(path, workspace=workspace)
    normalized = query.casefold()
    matches: list[SearchMatch] = []

    for file in repo.files:
        file_path = Path(file)
        if not _file_pattern_matches(file, normalized):
            continue

        matches.append(SearchMatch(path=file))
        if len(matches) >= limit:
            break

    total_matches = sum(1 for file in repo.files if _file_pattern_matches(file, normalized))
    return SearchResult(
        query=query,
        root=repo.root,
        method="repo_map",
        matches=matches,
        truncated=total_matches > len(matches) or repo.discovery_truncated,
    )


def grep_code(
    query: str,
    path: str = ".",
    file_pattern: str | None = None,
    case_sensitive: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
    *,
    workspace: str | Path = ".",
) -> str:
    result = search_code(
        query,
        path=path,
        file_pattern=file_pattern,
        case_sensitive=case_sensitive,
        max_results=max_results,
        workspace=workspace,
    )
    return result.format_for_llm()


def search_code(
    query: str,
    path: str = ".",
    file_pattern: str | None = None,
    case_sensitive: bool = False,
    max_results: int = DEFAULT_MAX_RESULTS,
    *,
    workspace: str | Path = ".",
) -> SearchResult:
    search_query = _validate_query(query)
    limit = _validate_max_results(max_results)
    repo = build_repo_map(path, workspace=workspace)

    if shutil.which("rg"):
        result = _search_with_rg(repo, search_query, file_pattern, case_sensitive, limit)
        if result is not None:
            return result

    return _search_with_python(repo, search_query, file_pattern, case_sensitive, limit)


def _search_with_rg(
    repo: RepoMap,
    query: str,
    file_pattern: str | None,
    case_sensitive: bool,
    max_results: int,
) -> SearchResult | None:
    command = ["rg", "--json", "--fixed-strings", "--color", "never"]
    if not case_sensitive:
        command.append("--ignore-case")

    if file_pattern:
        command.extend(["--glob", file_pattern])

    for ignored_name in sorted(IGNORED_NAMES):
        command.extend(["--glob", f"!**/{ignored_name}/**"])
    for ignored_pattern in sorted(IGNORED_PATTERNS):
        command.extend(["--glob", f"!**/{ignored_pattern}"])
        command.extend(["--glob", f"!**/{ignored_pattern}/**"])

    command.extend(["--", query, "."])
    try:
        completed = subprocess.run(
            command,
            cwd=repo.root,
            text=True,
            capture_output=True,
            timeout=SEARCH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if completed.returncode not in (0, 1):
        return None

    matches: list[SearchMatch] = []
    truncated = False
    for line in completed.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "match":
            continue

        data = event.get("data", {})
        path = data.get("path", {}).get("text")
        line_number = data.get("line_number")
        line_text = data.get("lines", {}).get("text", "")
        submatches = data.get("submatches", [])
        if not isinstance(path, str) or not isinstance(line_number, int):
            continue

        column = None
        if submatches and isinstance(submatches[0].get("start"), int):
            column = submatches[0]["start"] + 1

        match = SearchMatch(
            path=Path(path).as_posix(),
            line=line_number,
            column=column,
            preview=_preview(line_text),
        )
        if len(matches) >= max_results:
            truncated = True
            break

        matches.append(
            match
        )

    return SearchResult(query=query, root=repo.root, method="rg", matches=matches, truncated=truncated)


def _search_with_python(
    repo: RepoMap,
    query: str,
    file_pattern: str | None,
    case_sensitive: bool,
    max_results: int,
) -> SearchResult:
    matches: list[SearchMatch] = []
    normalized_query = query if case_sensitive else query.casefold()
    truncated = False

    for relative_path in repo.files:
        if file_pattern and not _glob_matches(relative_path, file_pattern):
            continue

        file_path = repo.root / relative_path
        try:
            if file_path.stat().st_size > MAX_FALLBACK_FILE_BYTES:
                continue
            raw_content = file_path.read_bytes()
        except OSError:
            continue

        if b"\x00" in raw_content:
            continue

        content = raw_content.decode(errors="replace")

        for line_number, line in enumerate(content.splitlines(), start=1):
            searchable = line if case_sensitive else line.casefold()
            column = searchable.find(normalized_query)
            if column < 0:
                continue

            match = SearchMatch(
                path=relative_path,
                line=line_number,
                column=column + 1,
                preview=_preview(line),
            )
            if len(matches) >= max_results:
                truncated = True
                return SearchResult(
                    query=query,
                    root=repo.root,
                    method="python",
                    matches=matches,
                    truncated=truncated or repo.discovery_truncated,
                )

            matches.append(match)

    return SearchResult(
        query=query,
        root=repo.root,
        method="python",
        matches=matches,
        truncated=truncated or repo.discovery_truncated,
    )


def _file_pattern_matches(file: str, normalized_pattern: str) -> bool:
    normalized_file = file.casefold()
    normalized_name = Path(file).name.casefold()
    return (
        normalized_pattern in normalized_file
        or fnmatch.fnmatch(normalized_file, normalized_pattern)
        or fnmatch.fnmatch(normalized_name, normalized_pattern)
    )


def _glob_matches(file: str, pattern: str) -> bool:
    return fnmatch.fnmatch(file, pattern) or fnmatch.fnmatch(Path(file).name, pattern)


def _preview(text: str) -> str:
    collapsed = " ".join(text.strip().split())
    if len(collapsed) <= MAX_PREVIEW_CHARS:
        return collapsed
    return collapsed[:MAX_PREVIEW_CHARS] + "..."


def _validate_query(query: str) -> str:
    normalized = query.strip()
    if not normalized:
        raise ValueError("Search query must not be empty.")
    return normalized


def _validate_max_results(max_results: int) -> int:
    if max_results < 1 or max_results > MAX_RESULTS:
        raise ValueError(f"max_results must be between 1 and {MAX_RESULTS}.")
    return max_results
