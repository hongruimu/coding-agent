from functools import partial
from pathlib import Path

from tools.filesystem import list_files, read_file, write_file
from tools.registry import Tool, ToolRegistry
from tools.shell import run_command


def create_registry(workspace: str | Path = ".") -> ToolRegistry:
    registry = ToolRegistry()

    registry.register(
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

    registry.register(
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
            function=partial(read_file, workspace=workspace),
        )
    )

    registry.register(
        Tool(
            name="write_file",
            description="Write complete text content to a file inside the workspace.",
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
            function=partial(write_file, workspace=workspace),
        )
    )

    registry.register(
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
