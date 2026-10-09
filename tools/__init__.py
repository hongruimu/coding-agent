from functools import partial
from pathlib import Path

from tools.filesystem import list_files, read_file, replace_text, write_file
from tools.git import git_diff
from tools.repo import repo_map
from tools.registry import Tool, ToolRegistry
from tools.search import grep_code, search_files
from tools.shell import run_command


def create_registry(
    workspace: str | Path = ".",
    allowed_tools: set[str] | None = None,
    allowed_write_paths: tuple[str, ...] | None = None,
    max_read_chars: int | None = None,
) -> ToolRegistry:
    registry = ToolRegistry()

    def register(tool: Tool) -> None:
        if allowed_tools is None or tool.name in allowed_tools:
            registry.register(tool)

    read_file_function = partial(read_file, workspace=workspace)
    if max_read_chars is not None:
        read_file_function = partial(read_file, workspace=workspace, max_chars=max_read_chars)

    register(
        Tool(
            name="repo_map",
            description=(
                "Return a filtered project map for the workspace, including project type, manifests, "
                "docs, source directories, test directories, entry points, and tracked files."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the workspace",
                        "default": ".",
                    }
                },
            },
            function=partial(repo_map, workspace=workspace),
        )
    )

    register(
        Tool(
            name="list_files",
            description="List files and directories under a workspace path.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the workspace",
                        "default": ".",
                    }
                },
            },
            function=partial(list_files, workspace=workspace),
        )
    )

    register(
        Tool(
            name="search_files",
            description="Search workspace file names and paths without reading file contents.",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Case-insensitive substring or glob pattern for file paths",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the workspace",
                        "default": ".",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results, from 1 to 200",
                        "default": 50,
                    },
                },
                "required": ["pattern"],
            },
            function=partial(search_files, workspace=workspace),
        )
    )

    register(
        Tool(
            name="grep_code",
            description="Search literal text in workspace files and return matching paths, lines, columns, and previews.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Literal text to search for",
                    },
                    "path": {
                        "type": "string",
                        "description": "Directory path relative to the workspace",
                        "default": ".",
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "Optional glob such as *.py or **/*.ts",
                    },
                    "case_sensitive": {
                        "type": "boolean",
                        "description": "Whether matching is case-sensitive",
                        "default": False,
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results, from 1 to 200",
                        "default": 50,
                    },
                },
                "required": ["query"],
            },
            function=partial(grep_code, workspace=workspace),
        )
    )

    register(
        Tool(
            name="read_file",
            description="Read the contents of a file.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path to the file",
                    }
                },
                "required": ["path"],
            },
            function=read_file_function,
        )
    )

    register(
        Tool(
            name="write_file",
            description="Create a new text file inside the workspace. Fails if the file already exists.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File path relative to the workspace",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full file content to write",
                    },
                },
                "required": ["path", "content"],
            },
            function=partial(write_file, workspace=workspace, allowed_paths=allowed_write_paths),
        )
    )

    register(
        Tool(
            name="replace_text",
            description=(
                "Modify an existing text file by replacing one unique exact text block. "
                "Read the file first and provide enough surrounding text to make the match unique."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Existing file path relative to the workspace",
                    },
                    "old_text": {
                        "type": "string",
                        "description": "Exact text block currently present exactly once",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Replacement text block",
                    },
                },
                "required": ["path", "old_text", "new_text"],
            },
            function=partial(replace_text, workspace=workspace, allowed_paths=allowed_write_paths),
        )
    )

    register(
        Tool(
            name="git_diff",
            description="Show the current Git diff for the workspace or a specific workspace path.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Optional file or directory path relative to the workspace",
                    }
                },
            },
            function=partial(git_diff, workspace=workspace),
        )
    )

    register(
        Tool(
            name="run_command",
            description="Run a shell command in the workspace and return stdout, stderr, and exit code.",
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Shell command to run from the workspace root",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds, from 1 to 120",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
            function=partial(run_command, workspace=workspace),
        )
    )

    return registry
