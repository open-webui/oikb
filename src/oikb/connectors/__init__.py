"""Base connector interface for oikb content sources."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """A single file in the source manifest.

    Attributes:
        filename: Basename of the file (e.g. "readme.md").
        path:     Directory path relative to source root (e.g. "docs/api").
                  Empty string for root-level files.
        checksum: SHA-256 hex digest of the file content.
        size:     File size in bytes.
    """

    filename: str
    path: str
    checksum: str
    size: int

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "path": self.path,
            "checksum": self.checksum,
            "size": self.size,
        }

    @property
    def display_path(self) -> str:
        """Human-readable relative path."""
        if self.path:
            return f"{self.path}/{self.filename}"
        return self.filename


class SourceFileUnavailable(Exception):
    """Raised when a source advertises a file but cannot provide its bytes."""


class BaseConnector(ABC):
    """Abstract base for all content source connectors.

    Every connector must implement two methods:
      - build_manifest(): enumerate all files with checksums
      - read_file(): return raw bytes for a specific file
    """

    @abstractmethod
    def build_manifest(self) -> list[ManifestEntry]:
        """Scan the source and return a manifest of all files.

        Returns:
            A list of ManifestEntry objects — one per file.
        """

    @abstractmethod
    def read_file(self, path: str, filename: str) -> bytes:
        """Read raw file content for upload.

        Args:
            path:     Directory path relative to source root.
            filename: Basename of the file.

        Returns:
            Raw bytes of the file content.
        """

    def close(self) -> None:
        """Release any resources (HTTP clients, etc.). No-op by default."""

    def __enter__(self) -> BaseConnector:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
