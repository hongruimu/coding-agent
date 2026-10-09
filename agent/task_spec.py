import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from tools.repo import RepoMap


FILE_REFERENCE_PATTERN = re.compile(
    r"(?<![\w.-])(?P<path>/?(?:[\w.@+-]+/)*[\w.@+-]+\.(?:py|js|jsx|ts|tsx|php|go|rs|java|kt|md|rst|txt|toml|json|ya?ml|ini|cfg|xml))",
    re.IGNORECASE,
)

READ_TOOLS = frozenset({"repo_map", "search_files", "grep_code", "list_files", "read_file"})


class TaskType(str, Enum):
    CREATE_README = "create_readme"
    EXPLAIN_PROJECT = "explain_project"
    MODIFY_CODE = "modify_code"
    FIX_TEST = "fix_test"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TaskSpec:
    raw_prompt: str
    workspace: Path
    task_type: TaskType
    target_files: tuple[str, ...]
    allowed_write_paths: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]
    write_allowed: bool
    shell_allowed: bool
    plan_required: bool

    def allowed_tools(self) -> set[str]:
        tools = set(READ_TOOLS)
        if self.write_allowed:
            tools.add("write_file")
        if self.shell_allowed:
            tools.add("run_command")
        return tools

    def format_for_prompt(self) -> str:
        payload = {
            "task_type": self.task_type.value,
            "workspace": str(self.workspace),
            "target_files": list(self.target_files),
            "allowed_write_paths": list(self.allowed_write_paths),
            "acceptance_criteria": list(self.acceptance_criteria),
            "write_allowed": self.write_allowed,
            "shell_allowed": self.shell_allowed,
            "plan_required": self.plan_required,
        }
        return "Task specification:\n" + json.dumps(payload, ensure_ascii=False, indent=2)


def build_task_spec(prompt: str, workspace: str | Path, repo_map: RepoMap) -> TaskSpec:
    workspace_root = Path(workspace).expanduser().resolve()
    task_type = _detect_task_type(prompt)
    target_files = _extract_target_files(prompt, workspace_root, repo_map.files)

    if task_type is TaskType.CREATE_README:
        readme_targets = tuple(path for path in target_files if Path(path).name.casefold().startswith("readme"))
        target_files = readme_targets or ("README.md",)
        return TaskSpec(
            raw_prompt=prompt,
            workspace=workspace_root,
            task_type=task_type,
            target_files=target_files,
            allowed_write_paths=target_files,
            acceptance_criteria=(
                "Describe the project purpose using inspected repository evidence.",
                "Include installation and usage instructions when supported by the project files.",
                "Summarize the project structure and main entry points.",
                "Do not claim features that were not verified from the repository.",
            ),
            write_allowed=True,
            shell_allowed=False,
            plan_required=True,
        )

    if task_type is TaskType.EXPLAIN_PROJECT:
        return TaskSpec(
            raw_prompt=prompt,
            workspace=workspace_root,
            task_type=task_type,
            target_files=target_files,
            allowed_write_paths=(),
            acceptance_criteria=(
                "Explain the requested behavior or architecture using repository evidence.",
                "Reference the relevant files and avoid modifying the workspace.",
            ),
            write_allowed=False,
            shell_allowed=False,
            plan_required=False,
        )

    if task_type is TaskType.FIX_TEST:
        allowed_write_paths = target_files or ("**/*",)
        return TaskSpec(
            raw_prompt=prompt,
            workspace=workspace_root,
            task_type=task_type,
            target_files=target_files,
            allowed_write_paths=allowed_write_paths,
            acceptance_criteria=(
                "Identify the root cause of the failing behavior.",
                "Apply the smallest relevant code or test change.",
                "Run the most relevant test or validation command and report its result.",
            ),
            write_allowed=True,
            shell_allowed=True,
            plan_required=True,
        )

    if task_type is TaskType.MODIFY_CODE:
        allowed_write_paths = target_files or ("**/*",)
        return TaskSpec(
            raw_prompt=prompt,
            workspace=workspace_root,
            task_type=task_type,
            target_files=target_files,
            allowed_write_paths=allowed_write_paths,
            acceptance_criteria=(
                "Implement the requested behavior with focused changes.",
                "Preserve unrelated behavior and project conventions.",
                "Run relevant validation and report the result.",
            ),
            write_allowed=True,
            shell_allowed=True,
            plan_required=True,
        )

    return TaskSpec(
        raw_prompt=prompt,
        workspace=workspace_root,
        task_type=TaskType.UNKNOWN,
        target_files=target_files,
        allowed_write_paths=(),
        acceptance_criteria=(
            "Inspect the repository and explain what information is needed before making changes.",
            "Do not modify files or execute shell commands until the task intent is clear.",
        ),
        write_allowed=False,
        shell_allowed=False,
        plan_required=False,
    )


def _detect_task_type(prompt: str) -> TaskType:
    normalized = prompt.casefold()

    if _contains_any(normalized, ("readme", "项目说明", "说明文档")) and _contains_any(
        normalized, ("创建", "新建", "生成", "编写", "写", "更新", "完善", "修改", "create", "generate", "write", "update")
    ):
        return TaskType.CREATE_README

    if _contains_any(normalized, ("测试失败", "测试报错", "修复测试", "fix test", "failing test", "test failure")):
        return TaskType.FIX_TEST

    if _contains_any(normalized, ("解释", "分析", "梳理", "介绍", "overview", "explain", "architecture", "架构")):
        return TaskType.EXPLAIN_PROJECT

    if _contains_any(
        normalized,
        ("实现", "添加", "新增", "修改", "重构", "修复", "create", "implement", "add", "update", "modify", "refactor", "fix"),
    ):
        return TaskType.MODIFY_CODE

    return TaskType.UNKNOWN


def _extract_target_files(prompt: str, workspace: Path, repo_files: list[str]) -> tuple[str, ...]:
    candidates: list[str] = []
    prompt_casefold = prompt.casefold()

    for match in FILE_REFERENCE_PATTERN.finditer(prompt):
        normalized = _normalize_target_path(match.group("path"), workspace)
        if normalized:
            candidates.append(normalized)

    basename_map: dict[str, list[str]] = {}
    for file in repo_files:
        basename_map.setdefault(Path(file).name.casefold(), []).append(file)
        if file.casefold() in prompt_casefold:
            candidates.append(file)

    for basename, matches in basename_map.items():
        if len(matches) == 1 and basename in prompt_casefold:
            candidates.append(matches[0])

    return tuple(dict.fromkeys(candidates))


def _normalize_target_path(value: str, workspace: Path) -> str | None:
    candidate = Path(value).expanduser()
    absolute = candidate.resolve() if candidate.is_absolute() else (workspace / candidate).resolve()
    try:
        return absolute.relative_to(workspace).as_posix()
    except ValueError:
        return None


def _contains_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)
