"""Bounded inspection and targeted file changes within a selected workspace."""

import hashlib
import json
import os
import stat
import tempfile
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

WORKSPACE_WRITE_TOOLS = [
    {
        "name": "create_workspace_file",
        "description": "Create a new UTF-8 file, including parent folders. Never overwrites an existing file. Paths must be workspace-relative.",
        "input_schema": {
            "type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"], "additionalProperties": False,
        },
    },
    {
        "name": "edit_workspace_file",
        "description": (
            "Edit a file you have read using read_workspace_file. Replace exactly one occurrence of old_text "
            "with new_text. Include unique surrounding context in old_text, without line-number prefixes. "
            "Fails if the file changed since you read it. UTF-8 only, maximum 1 MiB."
        ),
        "input_schema": {
            "type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"},
                                                 "new_text": {"type": "string"}},
            "required": ["path", "old_text", "new_text"], "additionalProperties": False,
        },
    },
]


class Workspace:
    def __init__(self, root: str | Path, *, allow_writes: bool = False):
        if not str(root).strip():
            raise ValueError("Provide a non-empty workspace path.")
        self.root = Path(root).expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("Workspace must be an existing directory.")
        self.allow_writes = allow_writes
        self._read_versions = {}
        self.changed_files = []

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

    def _resolve(self, path: str, *, strict: bool = True) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("Provide a non-empty workspace-relative path.")
        relative = Path(path)
        if relative.is_absolute() or relative.drive:
            raise ValueError("Tool paths must be relative to the selected workspace.")
        if self._excluded(relative):
            raise ValueError("This path is excluded from workspace inspection.")
        resolved = (self.root / relative).resolve(strict=strict)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Path is outside the selected workspace.")
        # Check the resolved target too, so an alias cannot expose an excluded file.
        if self._excluded(resolved.relative_to(self.root)):
            raise ValueError("This path is excluded from workspace inspection.")
        return resolved

    def _write_path(self, path: str, *, strict: bool = True) -> Path:
        if not self.allow_writes:
            raise ValueError("This agent has read-only workspace access.")
        target = self._resolve(path, strict=strict)
        # Reject aliases for writes, including Windows junctions and symlinks.
        current = self.root
        for part in Path(path).parts:
            if part in ("..", "."):
                raise ValueError("Write paths must not contain parent traversal.")
            current = current / part
            if current.is_symlink() or (current.exists() and current.resolve() != current):
                raise ValueError("Writes through filesystem aliases are not supported.")
        return target

    def _record_change(self, source: Path, action: str, raw: bytes) -> dict:
        path = source.relative_to(self.root).as_posix()
        self._read_versions[source] = hashlib.sha256(raw).digest()
        entry = {"path": path, "action": action}
        if not any(change["path"] == path for change in self.changed_files):
            self.changed_files.append(entry)
        return {**entry, "changed": True, "bytes": len(raw)}

    def create_file(self, path: str, content: str) -> dict:
        target = self._write_path(path, strict=False)
        if not isinstance(content, str) or "\x00" in content:
            raise ValueError("File content must be UTF-8 text without NUL characters.")
        raw = content.encode("utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("File exceeds the 1 MiB write limit.")
        target.parent.mkdir(parents=True, exist_ok=True)
        self._write_path(path, strict=False)
        with target.open("xb") as handle:
            handle.write(raw)
        return self._record_change(target, "created", raw)

    def edit_file(self, path: str, old_text: str, new_text: str) -> dict:
        target = self._write_path(path)
        if not target.is_file():
            raise ValueError("The edit path must be a regular file.")
        if not isinstance(old_text, str) or not old_text or not isinstance(new_text, str):
            raise ValueError("Provide non-empty old_text and a string new_text.")
        if "\x00" in old_text or "\x00" in new_text:
            raise ValueError("Binary edits are not supported.")
        with target.open("rb") as handle:
            original = handle.read(MAX_FILE_BYTES + 1)
        if len(original) > MAX_FILE_BYTES or b"\x00" in original:
            raise ValueError("Only UTF-8 text files up to 1 MiB can be edited.")
        version = hashlib.sha256(original).digest()
        if self._read_versions.get(target) != version:
            raise ValueError("Read this file before editing; it is unread or changed since the last read.")
        text = original.decode("utf-8-sig")
        newline = "\r\n" if "\r\n" in text and "\n" not in text.replace("\r\n", "") else "\n"
        if old_text not in text:
            old_text = old_text.replace("\r\n", "\n").replace("\n", newline)
        new_text = new_text.replace("\r\n", "\n").replace("\n", newline)
        if text.count(old_text) != 1:
            raise ValueError("old_text must match exactly once; include unique surrounding context.")
        updated = text.replace(old_text, new_text, 1)
        raw = updated.encode("utf-8-sig" if original.startswith(b"\xef\xbb\xbf") else "utf-8")
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("Edited file exceeds the 1 MiB write limit.")
        if raw == original:
            return {"path": target.relative_to(self.root).as_posix(), "changed": False}
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".agent-edit-", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(raw)
            os.chmod(temporary, stat.S_IMODE(target.stat().st_mode))
            self._write_path(path)
            if target.read_bytes() != original:
                raise ValueError("File changed during editing; read it again before retrying.")
            os.replace(temporary, target)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return self._record_change(target, "modified", raw)

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
        self._read_versions[source] = hashlib.sha256(raw).digest()
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
        if self.allow_writes:
            handlers.update(create_workspace_file=self.create_file, edit_workspace_file=self.edit_file)
        if name not in handlers:
            raise ValueError(f"Unknown workspace tool: {name}")
        if not isinstance(arguments, dict):
            raise ValueError("Tool arguments must be an object.")
        return json.dumps(handlers[name](**arguments), ensure_ascii=False)
