import fnmatch
import json
import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path


IGNORED_NAMES = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "ENV",
    "__pycache__",
    "node_modules",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}

IGNORED_PATTERNS = {
    "*.egg-info",
    "*.pyc",
    "*.pyo",
    "*.pyd",
    ".DS_Store",
}

MANIFEST_NAMES = {
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "requirements.txt",
    "package.json",
    "composer.json",
    "go.mod",
    "Cargo.toml",
    "pom.xml",
    "build.gradle",
    "tsconfig.json",
    "jsconfig.json",
    "vite.config.js",
    "vite.config.ts",
    "next.config.js",
    "next.config.ts",
}

README_NAMES = {"README.md", "README.rst", "README.txt", "readme.md"}
SOURCE_EXTENSIONS = {".py", ".js", ".jsx", ".ts", ".tsx", ".php", ".go", ".rs", ".java", ".kt"}
TEST_DIR_NAMES = {"test", "tests", "spec", "specs", "__tests__"}
ASSET_DIR_NAMES = {"assets", "public", "static", "media"}
CONFIG_NAMES = {".gitignore", ".prettierrc", ".eslintrc", "ruff.toml", "mypy.ini", "pytest.ini"}
MAX_OUTPUT_FILES = 300
MAX_DISCOVERED_FILES = 5_000
GIT_TIMEOUT_SECONDS = 10


@dataclass(frozen=True)
class FileDiscovery:
    files: list[str]
    method: str
    truncated: bool


@dataclass(frozen=True)
class RepoMap:
    workspace: Path
    root: Path
    discovery_method: str
    project_types: list[str]
    total_files: int
    manifests: list[str]
    docs: list[str]
    source_dirs: list[str]
    test_dirs: list[str]
    entry_points: list[str]
    important_files: list[str]
    files: list[str]
    files_truncated: bool
    discovery_truncated: bool

    def format_for_llm(self) -> str:
        sections = [
            f"workspace: {self.workspace}",
            f"analyzed_path: {self.root}",
            f"discovery_method: {self.discovery_method}",
            f"project_types: {', '.join(self.project_types) if self.project_types else 'unknown'}",
            f"total_files: {self.total_files}",
            _format_section("manifests", self.manifests),
            _format_section("docs", self.docs),
            _format_section("source_dirs", self.source_dirs),
            _format_section("test_dirs", self.test_dirs),
            _format_section("entry_points", self.entry_points),
            _format_section("important_files", self.important_files),
            _format_section("files", self.files[:MAX_OUTPUT_FILES]),
        ]

        if self.files_truncated:
            sections.append(f"files_truncated_after: {MAX_OUTPUT_FILES}")

        if self.discovery_truncated:
            sections.append(f"discovery_truncated_after: {MAX_DISCOVERED_FILES}")

        return "\n".join(sections)


def repo_map(path: str = ".", *, workspace: str | Path = ".") -> str:
    root = _safe_path(path, workspace)
    if not root.exists():
        return f"workspace: {_workspace_root(workspace)}\nDirectory not found: {path}"

    if not root.is_dir():
        return f"workspace: {_workspace_root(workspace)}\nNot a directory: {path}"

    return build_repo_map(path, workspace=workspace).format_for_llm()


def build_repo_map(path: str = ".", *, workspace: str | Path = ".") -> RepoMap:
    workspace_root = _workspace_root(workspace)
    root = _safe_path(path, workspace_root)
    discovery = _discover_files(root)
    files = _sort_files(discovery.files)
    manifest_files = _filter_by_name(files, MANIFEST_NAMES)
    readme_files = _filter_by_name(files, README_NAMES)
    test_dirs = _find_test_dirs(files)
    source_dirs = _find_source_dirs(files)
    entry_points = _find_entry_points(root, files)
    project_types = _detect_project_types(files)
    important_files = _important_files(files, manifest_files, readme_files, entry_points)

    return RepoMap(
        workspace=workspace_root,
        root=root,
        discovery_method=discovery.method,
        project_types=project_types,
        total_files=len(files),
        manifests=manifest_files,
        docs=readme_files,
        source_dirs=source_dirs,
        test_dirs=test_dirs,
        entry_points=entry_points,
        important_files=important_files,
        files=files,
        files_truncated=len(files) > MAX_OUTPUT_FILES,
        discovery_truncated=discovery.truncated,
    )


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
    directory = requested_path.resolve() if requested_path.is_absolute() else (root / requested_path).resolve()

    try:
        directory.relative_to(root)
    except ValueError:
        raise ValueError(f"Path escapes workspace: {path}") from None

    return directory


def _discover_files(root: Path) -> FileDiscovery:
    git_files = _git_files(root)
    if git_files is not None:
        return FileDiscovery(files=git_files, method="git", truncated=False)

    files: list[str] = []
    truncated = False
    for current_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [dirname for dirname in dirnames if not _is_ignored_part(dirname)]

        for filename in filenames:
            if _is_ignored_part(filename):
                continue

            relative = (Path(current_root) / filename).relative_to(root).as_posix()
            if _is_ignored(relative):
                continue

            files.append(relative)
            if len(files) >= MAX_DISCOVERED_FILES:
                truncated = True
                return FileDiscovery(files=_sort_files(files), method="walk", truncated=truncated)

    return FileDiscovery(files=_sort_files(files), method="walk", truncated=truncated)


def _git_files(root: Path) -> list[str] | None:
    git_root = _git_root(root)
    if git_root is None:
        return None

    try:
        result = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=git_root,
            text=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    files: list[str] = []
    for line in result.stdout.splitlines():
        if not line:
            continue

        absolute = (git_root / line).resolve()
        if not absolute.is_file():
            continue

        try:
            relative = absolute.relative_to(root).as_posix()
        except ValueError:
            continue

        if not _is_ignored(relative):
            files.append(relative)

    return _sort_files(files)


def _git_root(root: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None

    if result.returncode != 0:
        return None

    return Path(result.stdout.strip()).resolve()


def _is_ignored(relative_path: str) -> bool:
    return any(_is_ignored_part(part) for part in Path(relative_path).parts)


def _is_ignored_part(part: str) -> bool:
    if part in IGNORED_NAMES:
        return True

    return any(fnmatch.fnmatch(part, pattern) for pattern in IGNORED_PATTERNS)


def _filter_by_name(files: list[str], names: set[str]) -> list[str]:
    return [file for file in files if Path(file).name in names]


def _find_test_dirs(files: list[str]) -> list[str]:
    dirs: set[str] = set()
    for file in files:
        parts = Path(file).parts
        for index, part in enumerate(parts[:-1]):
            if part in TEST_DIR_NAMES:
                dirs.add(_join_dir(parts[: index + 1]))

    return sorted(dirs)


def _find_source_dirs(files: list[str]) -> list[str]:
    dirs: set[str] = set()
    for file in files:
        path = Path(file)
        if path.suffix not in SOURCE_EXTENSIONS or len(path.parts) < 2:
            continue

        top_level = path.parts[0]
        if top_level in TEST_DIR_NAMES or top_level in IGNORED_NAMES or top_level in ASSET_DIR_NAMES:
            continue

        dirs.add(f"{top_level}/")

    return sorted(dirs)


def _find_entry_points(root: Path, files: list[str]) -> list[str]:
    entry_points: list[str] = []
    for file in files:
        name = Path(file).name
        path = root / file
        if name == "pyproject.toml":
            entry_points.extend(_python_project_scripts(path))
        elif name == "package.json":
            entry_points.extend(_package_json_entry_points(path))
        elif name == "composer.json":
            entry_points.extend(_composer_entry_points(path))

    return sorted(set(entry_points))


def _python_project_scripts(path: Path) -> list[str]:
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return []

    scripts = data.get("project", {}).get("scripts", {})
    return [f"python script {name} = {target}" for name, target in sorted(scripts.items())]


def _package_json_entry_points(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ["package.json"]

    entry_points = ["package.json"]
    scripts = data.get("scripts", {})
    if isinstance(scripts, dict):
        entry_points.extend(f"npm script {name} = {target}" for name, target in sorted(scripts.items()))

    for key in ("main", "module", "types"):
        value = data.get(key)
        if isinstance(value, str):
            entry_points.append(f"package {key} = {value}")

    bin_value = data.get("bin")
    if isinstance(bin_value, str):
        entry_points.append(f"package bin = {bin_value}")
    elif isinstance(bin_value, dict):
        entry_points.extend(f"package bin {name} = {target}" for name, target in sorted(bin_value.items()))

    exports = data.get("exports")
    if isinstance(exports, str):
        entry_points.append(f"package exports = {exports}")
    elif isinstance(exports, dict):
        entry_points.extend(f"package export {name} = {target}" for name, target in _flatten_json_strings(exports))

    return entry_points


def _composer_entry_points(path: Path) -> list[str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return ["composer.json"]

    entry_points = ["composer.json"]
    scripts = data.get("scripts", {})
    if isinstance(scripts, dict):
        entry_points.extend(f"composer script {name} = {target}" for name, target in sorted(scripts.items()))

    autoload = data.get("autoload", {})
    if isinstance(autoload, dict):
        psr4 = autoload.get("psr-4", {})
        if isinstance(psr4, dict):
            entry_points.extend(f"composer autoload {namespace} = {target}" for namespace, target in sorted(psr4.items()))

    return entry_points


def _detect_project_types(files: list[str]) -> list[str]:
    types: list[str] = []
    if any(Path(file).name in {"pyproject.toml", "setup.py", "requirements.txt"} for file in files):
        types.append("python")
    if any(Path(file).name == "package.json" for file in files):
        types.append("node")
    if _has_extension(files, {".ts", ".tsx"}) or any(Path(file).name == "tsconfig.json" for file in files):
        types.append("typescript")
    if _has_extension(files, {".js", ".jsx"}) or any(Path(file).name in {"package.json", "jsconfig.json"} for file in files):
        types.append("javascript")
    if _has_extension(files, {".php"}) or any(Path(file).name == "composer.json" for file in files):
        types.append("php")
    if any(Path(file).name == "go.mod" for file in files):
        types.append("go")
    if any(Path(file).name == "Cargo.toml" for file in files):
        types.append("rust")
    if any(Path(file).name in {"pom.xml", "build.gradle"} for file in files):
        types.append("jvm")

    return types


def _has_extension(files: list[str], extensions: set[str]) -> bool:
    return any(Path(file).suffix in extensions for file in files)


def _important_files(files: list[str], manifest_files: list[str], readme_files: list[str], entry_points: list[str]) -> list[str]:
    important = set(manifest_files + readme_files)
    for entry_point in entry_points:
        if " = " not in entry_point:
            continue

        target = entry_point.rsplit(" = ", maxsplit=1)[-1]
        if Path(target).suffix:
            important.add(target)

    for file in files:
        path = Path(file)
        if path.name in CONFIG_NAMES:
            important.add(file)

    return [file for file in files if file in important]


def _sort_files(files: list[str]) -> list[str]:
    return sorted(files, key=lambda file: (_file_priority(file), file))


def _file_priority(file: str) -> int:
    path = Path(file)
    if path.name in MANIFEST_NAMES:
        return 0
    if path.name in README_NAMES:
        return 1
    if path.name in CONFIG_NAMES:
        return 2
    if any(part in TEST_DIR_NAMES for part in path.parts):
        return 4
    if path.suffix in SOURCE_EXTENSIONS:
        return 3
    return 5


def _flatten_json_strings(data: dict, prefix: str = "") -> list[tuple[str, str]]:
    values: list[tuple[str, str]] = []
    for key, value in sorted(data.items()):
        name = f"{prefix}.{key}" if prefix else str(key)
        if isinstance(value, str):
            values.append((name, value))
        elif isinstance(value, dict):
            values.extend(_flatten_json_strings(value, name))

    return values


def _join_dir(parts: tuple[str, ...]) -> str:
    return "/".join(parts) + "/"


def _format_section(name: str, values: list[str]) -> str:
    if not values:
        return f"{name}: none"

    lines = [f"{name}:"]
    lines.extend(f"- {value}" for value in values)
    return "\n".join(lines)
