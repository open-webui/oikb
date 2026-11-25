"""Opt-in provenance header for synced text documents.

Open WebUI does not show custom file metadata in citations, so the only way to
give readers (and the model) a link back to the original document is to put it
into the text itself. With ``include-source-header: true`` a source's text files
start with::

    Source: https://example.atlassian.net/wiki/pages/viewpage.action?pageId=123
    Last edited: 2026-11-20T09:15:00.000Z

The header only appears for files whose connector reports a ``source_url`` or a
``last_modified`` value, and only for text formats where prepending lines cannot
corrupt the file. The values are folded into the checksum, so turning the option
on or off, or a changed URL, re-uploads the affected files once.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from urllib.parse import urlsplit

from oikb.connectors import BaseConnector, ManifestEntry

OPTION_KEY = "include-source-header"

_TEXT_EXTENSIONS = frozenset({".txt", ".md", ".markdown"})
_MAX_TIMESTAMP_LENGTH = 64


def include_source_header(entry: dict) -> bool:
    """Read the ``include-source-header`` option of a source entry."""
    value = entry.get(OPTION_KEY, False)
    if not isinstance(value, bool):
        raise ValueError(f"{OPTION_KEY} must be true or false, got: {value!r}")
    return value


def _is_single_line(value: str) -> bool:
    return value.isprintable() and value == value.strip()


def _valid_source_url(url: str | None) -> str | None:
    """Return the URL if it is a plain http(s) URL on a single line."""
    if not url or not _is_single_line(url) or " " in url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        return None
    return url


def _valid_timestamp(value: str | None) -> str | None:
    if not value or len(value) > _MAX_TIMESTAMP_LENGTH or not _is_single_line(value):
        return None
    return value


def build_source_header(entry: ManifestEntry) -> str | None:
    """Return the header lines for an entry, or None if there is nothing to add."""
    if os.path.splitext(entry.filename)[1].lower() not in _TEXT_EXTENSIONS:
        return None
    lines = []
    source_url = _valid_source_url(entry.source_url)
    if source_url:
        lines.append(f"Source: {source_url}")
    last_modified = _valid_timestamp(entry.last_modified)
    if last_modified:
        lines.append(f"Last edited: {last_modified}")
    if not lines:
        return None
    return "\n".join(lines) + "\n\n"


class SourceHeaderConnector(BaseConnector):
    """Wrap a connector so its text files start with a provenance header."""

    def __init__(self, inner: BaseConnector):
        self._inner = inner
        self._headers: dict[tuple[str, str], bytes] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        self._headers.clear()
        entries = []
        for entry in self._inner.build_manifest():
            header = build_source_header(entry)
            if header is None:
                entries.append(entry)
                continue
            self._headers[(entry.path, entry.filename)] = header.encode("utf-8")
            fingerprint = hashlib.sha256(f"{entry.checksum}\n{header}".encode()).hexdigest()[:16]
            entries.append(replace(entry, checksum=fingerprint))
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        content = self._inner.read_file(path, filename)
        header = self._headers.get((path, filename))
        if not content or header is None:
            # Empty content stays empty so the sync still skips it.
            return content
        return header + content

    def close(self) -> None:
        self._inner.close()
