"""Notion connector — sync Notion pages to a Knowledge Base.

Auth via NOTION_TOKEN env var (internal integration token).
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


def _rich_text_to_str(rich_text: list[dict[str, Any]]) -> str:
    return "".join(t.get("plain_text", "") for t in rich_text)


def _blocks_to_text(blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in blocks:
        bt = block.get("type", "")
        data = block.get(bt, {})
        if bt in ("paragraph", "quote", "callout", "toggle"):
            lines.append(_rich_text_to_str(data.get("rich_text", [])))
        elif bt in ("heading_1", "heading_2", "heading_3"):
            lines.append(f"{'#' * int(bt[-1])} {_rich_text_to_str(data.get('rich_text', []))}")
        elif bt == "bulleted_list_item":
            lines.append(f"- {_rich_text_to_str(data.get('rich_text', []))}")
        elif bt == "numbered_list_item":
            lines.append(f"1. {_rich_text_to_str(data.get('rich_text', []))}")
        elif bt == "to_do":
            c = "x" if data.get("checked") else " "
            lines.append(f"[{c}] {_rich_text_to_str(data.get('rich_text', []))}")
        elif bt == "code":
            lang = data.get("language", "")
            lines.append(f"```{lang}\n{_rich_text_to_str(data.get('rich_text', []))}\n```")
        elif bt == "divider":
            lines.append("---")
    return "\n".join(lines)


class NotionConnector(BaseConnector):
    """Sync pages from a Notion database or page tree."""

    def __init__(self, root_id: str, token: str | None = None):
        self.root_id = root_id.replace("-", "")
        self._token = token or os.environ.get("NOTION_TOKEN")
        if not self._token:
            raise ValueError("Notion token required. Set NOTION_TOKEN env var.")

        self._http = httpx.Client(
            base_url="https://api.notion.com/v1",
            headers={"Authorization": f"Bearer {self._token}", "Notion-Version": "2022-06-28"},
            timeout=60.0,
        )
        self._page_cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        try:
            self._list_database(entries)
        except httpx.HTTPStatusError:
            self._list_children(self.root_id, "", entries)
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _list_database(self, entries: list[ManifestEntry]) -> None:
        cursor = None
        while True:
            body: dict[str, Any] = {"page_size": 100}
            if cursor:
                body["start_cursor"] = cursor
            resp = self._http.post(f"/databases/{self.root_id}/query", json=body)
            resp.raise_for_status()
            data = resp.json()
            for page in data.get("results", []):
                self._add_page(page, "", entries)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    def _list_children(self, page_id: str, prefix: str, entries: list[ManifestEntry]) -> None:
        cursor = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = self._http.get(f"/blocks/{page_id}/children", params=params)
            resp.raise_for_status()
            data = resp.json()
            for block in data.get("results", []):
                if block.get("type") == "child_page":
                    child_id = block["id"]
                    title = block.get("child_page", {}).get("title", "Untitled")
                    pr = self._http.get(f"/pages/{child_id}")
                    pr.raise_for_status()
                    self._add_page(pr.json(), prefix, entries)
                    sub = f"{prefix}/{title}" if prefix else title
                    self._list_children(child_id, sub, entries)
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")

    def _add_page(self, page: dict, prefix: str, entries: list[ManifestEntry]) -> None:
        page_id = page["id"]
        last_edited = page.get("last_edited_time", "")
        title = "Untitled"
        for prop in page.get("properties", {}).values():
            if prop.get("type") == "title":
                parts = prop.get("title", [])
                if parts:
                    title = _rich_text_to_str(parts)
                break
        checksum = hashlib.sha256(f"{page_id}:{last_edited}".encode()).hexdigest()[:16]
        filename = re.sub(r'[<>:"/\\|?*]', "_", title) + ".txt"
        entries.append(ManifestEntry(
            filename=filename,
            path=prefix,
            checksum=checksum,
            size=0,
            source_url=page.get("url"),
            last_modified=last_edited or None,
        ))
        key = f"{prefix}/{filename}" if prefix else filename
        self._page_cache[key] = page_id

    def read_file(self, path: str, filename: str) -> bytes:
        key = f"{path}/{filename}" if path else filename
        page_id = self._page_cache.get(key)
        if not page_id:
            raise FileNotFoundError(f"Page not found: {key}")
        blocks: list[dict] = []
        cursor = None
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if cursor:
                params["start_cursor"] = cursor
            resp = self._http.get(f"/blocks/{page_id}/children", params=params)
            resp.raise_for_status()
            data = resp.json()
            blocks.extend(data.get("results", []))
            if not data.get("has_more"):
                break
            cursor = data.get("next_cursor")
        return _blocks_to_text(blocks).encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_notion_source(source: str) -> dict[str, str | None]:
    root_id = source.removeprefix("notion:")
    if not root_id:
        raise ValueError("Invalid Notion source. Expected: notion:<id>")
    return {"root_id": root_id}
