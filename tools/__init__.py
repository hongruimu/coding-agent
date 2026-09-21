from tools.filesystem import read_file
from tools.registry import Tool, ToolRegistry


def create_registry() -> ToolRegistry:
    registry = ToolRegistry()

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
            function=read_file,
        )
    )

    return registry