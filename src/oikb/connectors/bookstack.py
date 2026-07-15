"""BookStack connector — sync pages from a BookStack instance as Markdown.

Auth via BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET env vars.
"""

from __future__ import annotations

import hashlib
import os
import re

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


class BookStackConnector(BaseConnector):
    """Sync pages from BookStack via its native Markdown export."""

    def __init__(self, base_url: str | None = None, token_id: str | None = None, token_secret: str | None = None):
        self._url = (base_url or os.environ.get("BOOKSTACK_URL", "")).rstrip("/")
        tid = token_id or os.environ.get("BOOKSTACK_TOKEN_ID", "")
        ts = token_secret or os.environ.get("BOOKSTACK_TOKEN_SECRET", "")
        if not self._url or not tid or not ts:
            raise ValueError("BookStack credentials required. Set BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET.")
        self._http = httpx.Client(base_url=self._url, headers={"Authorization": f"Token {tid}:{ts}"}, timeout=30.0)
        self._cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        offset = 0
        while True:
            resp = self._http.get("/api/pages", params={"count": 100, "offset": offset})
            resp.raise_for_status()
            data = resp.json()
            for page in data.get("data", []):
                title = re.sub(r'[<>:"/\\|?*]', "_", page.get("name", "Untitled"))
                filename = f"{page['id']}_{title[:50]}.md"
                checksum = hashlib.sha256(f"{page['id']}:{page.get('updated_at', '')}".encode()).hexdigest()[:16]
                entries.append(ManifestEntry(filename=filename, path="", checksum=checksum, size=0))
            if len(data.get("data", [])) < 100:
                break
            offset += 100
        entries.sort(key=lambda e: e.display_path)
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        page_id = filename.split("_")[0]
        resp = self._http.get(f"/api/pages/{page_id}/export/markdown")
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._http.close()


def parse_bookstack_source(source: str) -> dict[str, str | None]:
    return {}
