import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from tools.repo import RepoMap


MAX_VALIDATION_OUTPUT_CHARS = 12_000
PYTHON_SYNTAX_CHECK = (
    "import pathlib, sys; "
    "[compile(pathlib.Path(path).read_bytes(), path, 'exec') for path in sys.argv[1:]]"
)


@dataclass(frozen=True)
class ValidationCommand:
    name: str
    argv: tuple[str, ...]
    timeout: int = 60
    cwd: str = "."

    def display(self) -> str:
        return shlex.join(self.argv)


@dataclass(frozen=True)
class ValidationPlan:
    commands: tuple[ValidationCommand, ...]
    reason: str
    required: bool

    def format_for_prompt(self) -> str:
        payload = {
            "reason": self.reason,
            "required": self.required,
            "commands": [
                {
                    "name": command.name,
                    "command": command.display(),
                    "timeout": command.timeout,
                    "cwd": command.cwd,
                }
                for command in self.commands
            ],
        }
        return "Validation plan:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class ValidationResult:
    name: str
    argv: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cwd: str = "."

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def format_for_prompt(self) -> str:
        payload = {
            "name": self.name,
            "command": shlex.join(self.argv),
            "exit_code": self.exit_code,
            "passed": self.passed,
            "timed_out": self.timed_out,
            "cwd": self.cwd,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)


def build_validation_plan(repo_map: RepoMap, changed_files: tuple[str, ...]) -> ValidationPlan:
    changed = tuple(dict.fromkeys(changed_files))
    if not changed:
        return ValidationPlan(commands=(), reason="No files were changed.", required=False)

    commands: list[ValidationCommand] = []
    if repo_map.discovery_method == "git":
        commands.append(
            ValidationCommand(
                name="git diff check",
                argv=("git", "diff", "--check", "--", *changed),
                timeout=20,
            )
        )

    existing_changed = tuple(path for path in changed if (repo_map.workspace / path).is_file())
    python_files = tuple(path for path in existing_changed if Path(path).suffix.casefold() == ".py")
    if python_files:
        commands.append(
            ValidationCommand(
                name="python syntax check",
                argv=(sys.executable, "-c", PYTHON_SYNTAX_CHECK, *python_files),
                timeout=60,
            )
        )
        commands.extend(_python_test_commands(repo_map, python_files))

    web_files = tuple(path for path in existing_changed if Path(path).suffix.casefold() in {".js", ".jsx", ".ts", ".tsx"})
    web_config_changed = any(Path(path).name in {"package.json", "tsconfig.json", "jsconfig.json"} for path in changed)
    if web_files or web_config_changed:
        node_command = _node_validation_command(repo_map)
        if node_command is not None:
            commands.append(node_command)

    php_files = tuple(path for path in existing_changed if Path(path).suffix.casefold() == ".php")
    commands.extend(
        ValidationCommand(name=f"php syntax check: {path}", argv=("php", "-l", path), timeout=30)
        for path in php_files[:10]
    )

    go_manifest = _manifest_path(repo_map, "go.mod")
    if go_manifest is not None and (
        any(Path(path).suffix.casefold() == ".go" for path in existing_changed)
        or any(Path(path).name == "go.mod" for path in changed)
    ):
        commands.append(
            ValidationCommand(
                name="go tests",
                argv=("go", "test", "./..."),
                timeout=120,
                cwd=_parent_directory(go_manifest),
            )
        )

    rust_manifest = _manifest_path(repo_map, "Cargo.toml")
    if rust_manifest is not None and (
        any(Path(path).suffix.casefold() == ".rs" for path in existing_changed)
        or any(Path(path).name == "Cargo.toml" for path in changed)
    ):
        commands.append(
            ValidationCommand(
                name="rust tests",
                argv=("cargo", "test"),
                timeout=120,
                cwd=_parent_directory(rust_manifest),
            )
        )

    reason = "Selected validation from changed file types and available project manifests."
    if not commands:
        reason = "No supported automatic validation was found for the changed files."
    return ValidationPlan(commands=tuple(commands), reason=reason, required=bool(commands))


def execute_validation_plan(plan: ValidationPlan, workspace: str | Path) -> tuple[ValidationResult, ...]:
    root = Path(workspace).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        raise ValueError(f"Workspace is not a directory: {root}")

    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["CI"] = "1"
    results = []
    for command in plan.commands:
        command_root = (root / command.cwd).resolve()
        try:
            command_root.relative_to(root)
        except ValueError:
            results.append(_invalid_cwd_result(command, f"Validation cwd escapes workspace: {command.cwd}"))
            continue
        if not command_root.is_dir():
            results.append(_invalid_cwd_result(command, f"Validation cwd is not a directory: {command.cwd}"))
            continue
        try:
            completed = subprocess.run(
                list(command.argv),
                cwd=command_root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=command.timeout,
                shell=False,
            )
            result = ValidationResult(
                name=command.name,
                argv=command.argv,
                exit_code=completed.returncode,
                stdout=_truncate(completed.stdout),
                stderr=_truncate(completed.stderr),
                cwd=command.cwd,
            )
        except subprocess.TimeoutExpired as exc:
            result = ValidationResult(
                name=command.name,
                argv=command.argv,
                exit_code=124,
                stdout=_truncate(_to_text(exc.stdout)),
                stderr=_truncate(_to_text(exc.stderr)),
                timed_out=True,
                cwd=command.cwd,
            )
        except OSError as exc:
            result = ValidationResult(
                name=command.name,
                argv=command.argv,
                exit_code=127,
                stdout="",
                stderr=str(exc),
                cwd=command.cwd,
            )
        results.append(result)
    return tuple(results)


def _python_test_commands(repo_map: RepoMap, changed_files: tuple[str, ...]) -> tuple[ValidationCommand, ...]:
    changed_keys = {_relation_key(path) for path in changed_files}
    tests = []
    for path in repo_map.files:
        if not _is_test_file(path) or Path(path).suffix.casefold() != ".py":
            continue
        if path in changed_files or _relation_key(path) in changed_keys:
            tests.append(path)

    selected_tests = tuple(dict.fromkeys(tests))[:5]
    if not selected_tests:
        return ()
    if _uses_pytest(repo_map):
        return (
            ValidationCommand(
                name="python related tests",
                argv=(sys.executable, "-m", "pytest", *selected_tests),
                timeout=120,
            ),
        )

    commands = []
    for path in selected_tests:
        test_path = Path(path)
        start_dir = test_path.parent.as_posix() or "."
        commands.append(
            ValidationCommand(
                name=f"python test: {path}",
                argv=(sys.executable, "-m", "unittest", "discover", "-s", start_dir, "-p", test_path.name),
                timeout=90,
            )
        )
    return tuple(commands)


def _uses_pytest(repo_map: RepoMap) -> bool:
    names = {Path(path).name for path in repo_map.files}
    if "pytest.ini" in names or "conftest.py" in names:
        return True

    candidates = [path for path in repo_map.files if Path(path).name in {"pyproject.toml", "requirements.txt"}]
    for path in candidates:
        try:
            content = (repo_map.root / path).read_text(errors="replace").casefold()
        except OSError:
            continue
        if "pytest" in content:
            return True
    return False


def _node_validation_command(repo_map: RepoMap) -> ValidationCommand | None:
    package_files = [path for path in repo_map.manifests if Path(path).name == "package.json"]
    if not package_files:
        return None

    package_file = package_files[0]
    package_path = repo_map.root / package_file
    try:
        package = json.loads(package_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None

    scripts = package.get("scripts", {})
    if not isinstance(scripts, dict):
        return None
    script = next((name for name in ("test", "typecheck", "lint", "build") if isinstance(scripts.get(name), str)), None)
    if script is None:
        return None

    package_directory = Path(package_file).parent
    files = set(repo_map.files)
    if (package_directory / "pnpm-lock.yaml").as_posix() in files:
        argv = ("pnpm", "run", script)
    elif (package_directory / "yarn.lock").as_posix() in files:
        argv = ("yarn", script)
    else:
        argv = ("npm", "run", script)
    return ValidationCommand(
        name=f"node script: {script}",
        argv=argv,
        timeout=120,
        cwd=package_directory.as_posix(),
    )


def _manifest_path(repo_map: RepoMap, name: str) -> str | None:
    return next((path for path in repo_map.manifests if Path(path).name == name), None)


def _parent_directory(path: str) -> str:
    parent = Path(path).parent.as_posix()
    return parent if parent != "." else "."


def _invalid_cwd_result(command: ValidationCommand, message: str) -> ValidationResult:
    return ValidationResult(
        name=command.name,
        argv=command.argv,
        exit_code=127,
        stdout="",
        stderr=message,
        cwd=command.cwd,
    )


def _is_test_file(path: str) -> bool:
    file_path = Path(path)
    return any(part.casefold() in {"test", "tests", "spec", "specs", "__tests__"} for part in file_path.parts[:-1]) or file_path.stem.casefold().startswith("test_")


def _relation_key(path: str) -> str:
    stem = Path(path).stem.casefold()
    for prefix in ("test_", "spec_"):
        if stem.startswith(prefix):
            stem = stem[len(prefix) :]
    for suffix in ("_test", "_spec", ".test", ".spec"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    return stem


def _truncate(text: str) -> str:
    if len(text) <= MAX_VALIDATION_OUTPUT_CHARS:
        return text
    return text[:MAX_VALIDATION_OUTPUT_CHARS] + f"\n... truncated after {MAX_VALIDATION_OUTPUT_CHARS} characters"


def _to_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
