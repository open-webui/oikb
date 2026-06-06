"""Filesystem connector — sync a local directory to a Knowledge Base."""

from __future__ import annotations

import hashlib
from pathlib import Path

from oikb.connectors import BaseConnector, ManifestEntry
from oikb.ignore import load_ignore_patterns, should_ignore

# Files and directories to always skip during traversal.
DEFAULT_IGNORE = frozenset({
    ".git",
    ".svn",
    ".hg",
    ".DS_Store",
    "Thumbs.db",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    ".oikb",
    ".env",
})


def _sha256(path: Path) -> str:
    """Compute SHA-256 hex digest of a file, reading in 64 KiB chunks."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65_536):
            h.update(chunk)
    return h.hexdigest()


class FilesystemConnector(BaseConnector):
    """Walk a local directory tree and produce a manifest.

    Respects:
      - Built-in ignore list (hidden files, .git, node_modules, etc.)
      - .oikbignore file in the root directory (gitignore-style patterns)
      - Custom ignore set passed via constructor

    Args:
        root:   Path to the directory to sync.
        ignore: Additional names to skip (files and dirs).
    """

    def __init__(
        self,
        root: str | Path,
        ignore: frozenset[str] | None = None,
    ):
        self.root = Path(root).resolve()
        self.builtin_ignore = ignore or DEFAULT_IGNORE
        self.ignore_patterns = load_ignore_patterns(self.root)

        if not self.root.is_dir():
            raise FileNotFoundError(f"Not a directory: {self.root}")

    def build_manifest(self) -> list[ManifestEntry]:
        """Recursively walk root, computing SHA-256 for each file."""
        entries: list[ManifestEntry] = []
        self._walk(self.root, "", entries)
        # Sort for deterministic output.
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _walk(
        self,
        directory: Path,
        relative_prefix: str,
        entries: list[ManifestEntry],
    ) -> None:
        """Recursive directory walker."""
        for child in sorted(directory.iterdir()):
            # Always skip built-in ignores.
            if child.name in self.builtin_ignore or child.name.startswith("."):
                continue

            relative_path = (
                f"{relative_prefix}/{child.name}" if relative_prefix else child.name
            )

            # Check .oikbignore patterns.
            if self.ignore_patterns and should_ignore(
                relative_path, child.name, child.is_dir(), self.ignore_patterns
            ):
                continue

            if child.is_dir():
                self._walk(child, relative_path, entries)
            elif child.is_file():
                entries.append(
                    ManifestEntry(
                        filename=child.name,
                        path=relative_prefix,
                        checksum=_sha256(child),
                        size=child.stat().st_size,
                    )
                )

    def read_file(self, path: str, filename: str) -> bytes:
        """Read a file from the local filesystem."""
        if path:
            file_path = self.root / path / filename
        else:
            file_path = self.root / filename
            
        resolved_path = file_path.resolve()
        if not resolved_path.is_relative_to(self.root):
            raise PermissionError(
                f"Path traversal attempt detected: {filename} resolves outside the root directory."
            )
            
        return resolved_path.read_bytes()
