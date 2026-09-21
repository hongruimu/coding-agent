from pathlib import Path

def read_file(path: str) -> str:
    file_path = Path(path)

    if not file_path.exists():
        return f"File not found: {path}"

    if not file_path.is_file():
        return f"Not a file: {path}"

    return file_path.read_text()