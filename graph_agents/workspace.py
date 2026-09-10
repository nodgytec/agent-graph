"""Bounded, read-only code inspection within a selected workspace."""

import json
from pathlib import Path


EXCLUDED_NAMES = {
    ".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", ".next", "dist", "build",
    ".ssh", ".aws", ".azure", ".npmrc", ".pypirc", ".netrc",
    "id_rsa", "id_ed25519", "credentials.json", "secrets.json",
}
EXCLUDED_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
MAX_FILE_BYTES = 1_048_576
MAX_READ_CHARS = 24_000
MAX_READ_LINES = 300
LIST_PAGE_SIZE = 200

WORKSPACE_TOOLS = [
    {
        "name": "list_workspace_files",
        "description": (
            "List one directory in the selected codebase. Paths are relative to the workspace. "
            "Start with path='.'. Descend into relevant subdirectories as needed. "
            "Returns up to 200 entries; use next_offset for more."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "offset": {"type": "integer", "minimum": 0, "default": 0},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_workspace_file",
        "description": (
            "Read a UTF-8 source file using a workspace-relative path. Output includes line "
            "numbers. Read at most 300 lines per call; request further ranges as needed. "
            "Binary files, files over 1 MiB, credentials, and generated directories are excluded."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "start_line": {"type": "integer", "minimum": 1, "default": 1},
                "end_line": {"type": "integer", "minimum": 1, "default": 200},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
]


class Workspace:
    def __init__(self, root: str | Path):
        if not str(root).strip():
            raise ValueError("Provide a non-empty workspace path.")
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be an existing directory.")

    @staticmethod
    def _excluded(path: Path) -> bool:
        for part in path.parts:
            name = part.lower().rstrip(" .")
            if (
                name in EXCLUDED_NAMES or name.startswith(".env")
                or Path(name).suffix in EXCLUDED_SUFFIXES or ":" in name
            ):
                return True
        return False

    def _resolve(self, path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Provide a non-empty workspace-relative path.")
        relative = Path(path)
        if relative.is_absolute() or relative.drive:
            raise ValueError("Tool paths must be relative to the selected workspace.")
        if self._excluded(relative):
            raise ValueError("This path is excluded from workspace inspection.")
        resolved = (self.root / relative).resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path is outside the selected workspace.")
        # Check the resolved target too, so an alias cannot expose an excluded file.
        if self._excluded(resolved.relative_to(self.root)):
            raise ValueError("This path is excluded from workspace inspection.")
        return resolved

    def list_files(self, path: str = ".", offset: int = 0) -> dict:
        if type(offset) is not int or offset < 0:
            raise ValueError("offset must be a non-negative integer.")
        directory = self._resolve(path)
        if not directory.is_dir():
            raise ValueError("The listing path must be a directory.")
        entries = []
        for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
            relative = child.relative_to(self.root)
            try:
                resolved = self._resolve(relative.as_posix())
                if resolved.is_dir():
                    kind = "directory"
                elif resolved.is_file():
                    kind = "file"
                else:
                    continue
            except (OSError, ValueError, RuntimeError):
                continue
            entries.append({"path": relative.as_posix(), "type": kind})
        end = offset + LIST_PAGE_SIZE
        return {
            "path": directory.relative_to(self.root).as_posix(),
            "entries": entries[offset:end],
            "next_offset": end if end < len(entries) else None,
        }

    def read_file(self, path: str, start_line: int = 1, end_line: int = 200) -> dict:
        if type(start_line) is not int or type(end_line) is not int or not 1 <= start_line <= end_line:
            raise ValueError("Use integer line numbers with 1 <= start_line <= end_line.")
        if end_line - start_line + 1 > MAX_READ_LINES:
            raise ValueError(f"Read at most {MAX_READ_LINES} lines per call.")
        source = self._resolve(path)
        if not source.is_file():
            raise ValueError("The read path must be a regular file.")
        with source.open("rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("File exceeds the 1 MiB inspection limit.")
        if b"\x00" in raw:
            raise ValueError("Binary files are not supported.")
        lines = raw.decode("utf-8-sig").splitlines()
        text = "\n".join(
            f"{number}: {line}"
            for number, line in enumerate(lines[start_line - 1:end_line], start=start_line)
        )
        return {
            "path": source.relative_to(self.root).as_posix(),
            "total_lines": len(lines),
            "content": text[:MAX_READ_CHARS],
            "truncated": len(text) > MAX_READ_CHARS,
        }

    def execute(self, name: str, arguments: dict) -> str:
        handlers = {"list_workspace_files": self.list_files, "read_workspace_file": self.read_file}
        if name not in handlers:
            raise ValueError(f"Unknown workspace tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        return json.dumps(handlers[name](**arguments), ensure_ascii=False)
