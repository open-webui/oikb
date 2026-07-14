"""BookStack connector — sync pages from a BookStack instance.

Auth via BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET env vars.
"""

from __future__ import annotations

import hashlib
import os
import re

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


class BookStackConnector(BaseConnector):
    """Sync pages from BookStack."""

    def __init__(
        self,
        base_url: str | None = None,
        token_id: str | None = None,
        token_secret: str | None = None,
        book_id: str | None = None,
    ):
        self._url = (base_url or os.environ.get("BOOKSTACK_URL", "")).rstrip("/")
        tid = token_id or os.environ.get("BOOKSTACK_TOKEN_ID", "")
        ts = token_secret or os.environ.get("BOOKSTACK_TOKEN_SECRET", "")
        if not self._url or not tid or not ts:
            raise ValueError("BookStack credentials required. Set BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET.")
        self._http = httpx.Client(base_url=self._url, headers={"Authorization": f"Token {tid}:{ts}"}, timeout=30.0)
        self._cache: dict[str, str] = {}
        self.book_id = book_id

    def build_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        offset = 0
        while True:
            params: dict[str, int | str] = {"count": 100, "offset": offset}
            if self.book_id:
                params["filter[book_id]"] = self.book_id
            resp = self._http.get("/api/pages", params=params)
            resp.raise_for_status()
            data = resp.json()
            for page in data.get("data", []):
                title = re.sub(r'[<>:"/\\|?*]', "_", page.get("name", "Untitled"))
                filename = f"{page['id']}_{title[:50]}.txt"
                checksum = hashlib.sha256(f"{page['id']}:{page.get('updated_at', '')}".encode()).hexdigest()[:16]
                entries.append(ManifestEntry(filename=filename, path="", checksum=checksum, size=0))
            if len(data.get("data", [])) < 100:
                break
            offset += 100
        entries.sort(key=lambda e: e.display_path)
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        page_id = filename.split("_")[0]
        resp = self._http.get(f"/api/pages/{page_id}")
        resp.raise_for_status()
        data = resp.json()
        text = f"# {data.get('name', '')}\n\n{re.sub(r'<[^>]+>', ' ', data.get('html', ''))}"
        return text.encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_bookstack_source(source: str) -> dict[str, str | None]:
    book_id = source.removeprefix("bookstack:")
    if not book_id:
        return {"book_id": None}

    if not book_id.isdecimal():
        raise ValueError("Invalid BookStack source. Expected a numeric book ID, e.g. bookstack:12")
    return {"book_id": book_id}
