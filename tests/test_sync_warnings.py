"""Unavailable source files are non-fatal warnings, every other failure is an error.

Covers the SourceFileUnavailable path end to end:
  - the Zotero connector maps a missing-file fetch to SourceFileUnavailable
  - run_sync routes that exception to result.warnings (not result.errors), so a run
    whose only problems are unavailable files still succeeds
  - any other read_file() exception still lands in result.errors
"""

from __future__ import annotations

import pytest

from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable
from oikb.sync import run_sync


class _FakeClient:
    """Minimal OikbClient stand-in: marks every manifest file as 'added'."""

    def __init__(self) -> None:
        self.uploaded: list[str] = []

    def sync_diff(self, kb_id: str, manifest: list[dict]) -> dict:
        return {
            "added": [{"filename": e["filename"], "path": e["path"]} for e in manifest],
            "modified": [],
            "deleted": [],
            "unmodified_count": 0,
            "mkdir": [],
            "rmdir": [],
            "directory_map": {},
        }

    def sync_cleanup(self, *a, **k) -> None:  # pragma: no cover - unused here
        pass

    def create_directory(self, *a, **k) -> dict:  # pragma: no cover - unused here
        return {"id": "dir"}

    def upload_file(self, *, filename: str, **k) -> None:
        self.uploaded.append(filename)

    def close(self) -> None:
        pass


class _Connector(BaseConnector):
    """Two-file source; reading `bad.txt` raises the exception given at construction."""

    def __init__(self, bad_exc: Exception) -> None:
        self._bad_exc = bad_exc

    def build_manifest(self) -> list[ManifestEntry]:
        return [
            ManifestEntry(filename="good.txt", path="", checksum="h-good", size=4),
            ManifestEntry(filename="bad.txt", path="", checksum="h-bad", size=0),
        ]

    def read_file(self, path: str, filename: str) -> bytes:
        if filename == "bad.txt":
            raise self._bad_exc
        return b"data"


def _sync(bad_exc: Exception) -> tuple:
    client = _FakeClient()
    result = run_sync(client=client, connector=_Connector(bad_exc), kb_id="kb", quiet=True)
    return client, result


def test_unavailable_source_file_is_a_warning_not_an_error():
    client, result = _sync(SourceFileUnavailable("no downloadable file for ABC123"))

    # The good file uploaded; the unavailable one was skipped, not retried into failure.
    assert client.uploaded == ["good.txt"]
    assert result.added == 1
    assert result.errors == []  # would have been exit 1 in the CLI
    assert result.warnings and "bad.txt" in result.warnings[0]
    # Surfaced in the human summary as a skip.
    assert "1 skipped" in result.summary()


def test_other_read_failure_is_still_an_error():
    client, result = _sync(RuntimeError("connection reset"))

    assert client.uploaded == ["good.txt"]
    assert result.added == 1
    assert result.warnings == []
    assert result.errors and "bad.txt" in result.errors[0]


def test_zotero_missing_file_maps_to_sourcefileunavailable():
    """A Zotero attachment with no fetchable bytes raises SourceFileUnavailable."""
    from oikb.connectors.zotero import ZoteroConnector

    # Build without __init__ so we don't need real credentials / pyzotero.
    conn = ZoteroConnector.__new__(ZoteroConnector)
    conn._text_cache = {}

    class _Zot:
        def fulltext_item(self, key):
            raise RuntimeError("not indexed")  # forces the file() fallback

        def file(self, key):
            raise RuntimeError("Code: 404 Not found")

    conn._zot = _Zot()

    with pytest.raises(SourceFileUnavailable):
        conn._extract_text("W3PU62BI")
