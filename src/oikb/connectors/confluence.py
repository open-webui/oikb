"""Confluence connector — sync a Confluence space to a Knowledge Base.

Supports Confluence REST API v1 (self-hosted) and v2 (Cloud). Pages are
exported as plain text. Select the API via CONFLUENCE_API_VERSION (default v2).

Auth via CONFLUENCE_URL, CONFLUENCE_USER, and CONFLUENCE_TOKEN env vars:
  - Server/Data Center PAT: set CONFLUENCE_TOKEN only (Bearer auth)
  - Cloud API token: set CONFLUENCE_USER (email) + CONFLUENCE_TOKEN (Basic auth)
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from typing import Any

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


BASE_ENDPOINTS = {
    "v1": "/rest/api",
    "v2": "/wiki/api/v2",
}


_INVALID_PATH_CHARS = re.compile(r'[<>:"/\\|?*]')


def _sanitize_path_segment(name: str) -> str:
    """Sanitize a single path segment (ancestor dir or filename stem)."""
    cleaned = _INVALID_PATH_CHARS.sub("_", name).strip()
    return cleaned or "_"


def _ancestor_dir_path_v1(page: dict[str, Any]) -> str:
    """Build KB directory path from Confluence v1 ancestors list."""
    segments = [
        _sanitize_path_segment(a.get("title", ""))
        for a in page.get("ancestors") or []
        if a.get("title")
    ]
    return "/".join(segments)


def _ancestor_dir_path_v2(
    page_id: str, pages_by_id: dict[str, dict[str, Any]]
) -> str:
    """Build KB directory path by walking Confluence v2 parentId chain."""
    segments: list[str] = []
    current = pages_by_id.get(str(page_id))
    if not current:
        return ""

    parent_id = current.get("parentId")
    visited: set[str] = set()
    while parent_id:
        pid = str(parent_id)
        if pid in visited:
            break
        visited.add(pid)
        parent = pages_by_id.get(pid)
        if not parent:
            break
        title = parent.get("title", "")
        if title:
            segments.append(_sanitize_path_segment(title))
        parent_id = parent.get("parentId")

    segments.reverse()
    return "/".join(segments)


def _parse_api_version(api_version: str | None) -> str:
    version = (api_version or os.environ.get("CONFLUENCE_API_VERSION", "v2")).lower()
    if version not in BASE_ENDPOINTS:
        valid = ", ".join(sorted(BASE_ENDPOINTS))
        raise ValueError(
            f"Invalid Confluence API version {version!r}. "
            f"Expected one of: {valid}. Set CONFLUENCE_API_VERSION=v1 or v2."
        )
    return version


def _storage_to_text(storage_html: str, title: str = "") -> str:
    """Convert Confluence storage format (XHTML) to plain text.

    Confluence link-only index pages store targets in XML attributes
    (ri:content-title, href) rather than visible text. Plain tag stripping
    yields an empty string and Open WebUI rejects the upload with 400.
    """
    parts: list[str] = []

    # Page / attachment link targets live in attributes, not element text.
    for pattern in (
        r'ri:content-title="([^"]*)"',
        r'ri:filename="([^"]*)"',
        r'ri:url="([^"]*)"',
    ):
        for match in re.finditer(pattern, storage_html):
            value = html.unescape(match.group(1)).strip()
            if value:
                parts.append(value)

    for match in re.finditer(r'href="([^"#][^"]*)"', storage_html):
        value = html.unescape(match.group(1)).strip()
        if value:
            parts.append(value)

    for match in re.finditer(
        r"<ac:plain-text-link-body>([^<]*)</ac:plain-text-link-body>",
        storage_html,
    ):
        value = html.unescape(match.group(1)).strip()
        if value:
            parts.append(value)

    # Remaining visible text after stripping tags/macros.
    text = re.sub(r"<[^>]+>", " ", storage_html)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if text:
        parts.append(text)

    # De-dupe while preserving order.
    seen: set[str] = set()
    lines: list[str] = []
    for part in parts:
        if part not in seen:
            seen.add(part)
            lines.append(part)

    if lines:
        return "\n".join(lines)

    return title.strip()


def _finalize_page_text(
    text: str,
    *,
    title: str,
    page_id: str,
    space_key: str,
    base_url: str,
) -> str:
    """Ensure non-empty, unique text for Open WebUI vector deduplication.

    OWUI hashes extracted content and rejects duplicates. Multiple Confluence
    pages (empty bodies, identical index pages, same content across spaces)
    would otherwise share the same hash — including e3b0c442… for "".
    """
    body = text.strip() or title.strip() or f"Confluence page {page_id}"
    source = f"confluence:{space_key}:{page_id}"
    if base_url:
        source = f"{source} {base_url.rstrip('/')}/pages/viewpage.action?pageId={page_id}"
    return f"{body}\n\n---\n{source}"


class ConfluenceConnector(BaseConnector):
    """Sync pages from a Confluence space.

    Args:
        space_key:   Confluence space key (e.g. "ENG").
        base_url:    Confluence instance URL (or CONFLUENCE_URL env var).
        user:        Confluence user email/username (or CONFLUENCE_USER env var).
        token:       API token or PAT (or CONFLUENCE_TOKEN env var).
        api_version: REST API version, "v1" or "v2" (or CONFLUENCE_API_VERSION env var).
    """

    def __init__(
        self,
        space_key: str,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
        api_version: str | None = None,
    ):
        self.space_key = space_key

        self._base_url = (base_url or os.environ.get("CONFLUENCE_URL", "")).rstrip("/")
        self._user = user or os.environ.get("CONFLUENCE_USER", "")
        self._token = token or os.environ.get("CONFLUENCE_TOKEN", "")
        self._api_version = _parse_api_version(api_version)

        headers = {"Accept": "application/json"}

        if not self._base_url:
            raise ValueError(
                "Confluence URL required. Set via:\n"
                "  export CONFLUENCE_URL=https://company.atlassian.net"
            )
        if not self._token:
            raise ValueError(
                "Confluence API token required. Set via:\n"
                "  export CONFLUENCE_TOKEN=<api_token>"
            )

        if not self._user:
            headers["Authorization"] = f"Bearer {self._token}"

        self._http = httpx.Client(
            base_url=f"{self._base_url}{BASE_ENDPOINTS[self._api_version]}",
            auth=(self._user, self._token) if self._user else None,
            headers=headers,
            timeout=60.0,
            follow_redirects=False,
        )

        # (path, filename) -> Confluence page id
        self._page_cache: dict[tuple[str, str], str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        """List all pages in the space and build a manifest."""
        if self._api_version == "v2":
            return self._build_manifest_v2()
        return self._build_manifest_v1()

    def _build_manifest_v1(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        used_keys: set[tuple[str, str]] = set()
        start = 0
        limit = 250

        while True:
            params: dict[str, Any] = {
                "spaceKey": self.space_key,
                "type": "page",
                "limit": limit,
                "start": start,
                "expand": "ancestors,version",
            }

            resp = self._http.get("/content", params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            if not results:
                break

            for page in results:
                dir_path = _ancestor_dir_path_v1(page)
                self._add_page_entry(entries, page, dir_path, used_keys)

            if len(results) < limit:
                break
            start += len(results)

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _build_manifest_v2(self) -> list[ManifestEntry]:
        all_pages: list[dict[str, Any]] = []
        cursor = None

        while True:
            params: dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor

            resp = self._http.get(
                f"/spaces/{self.space_key}/pages",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            all_pages.extend(data.get("results", []))

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            cursor_match = re.search(r"cursor=([^&]+)", next_link)
            cursor = cursor_match.group(1) if cursor_match else None
            if not cursor:
                break

        pages_by_id = {str(page["id"]): page for page in all_pages}
        entries: list[ManifestEntry] = []
        used_keys: set[tuple[str, str]] = set()
        for page in all_pages:
            dir_path = _ancestor_dir_path_v2(str(page["id"]), pages_by_id)
            self._add_page_entry(entries, page, dir_path, used_keys)

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _unique_path_filename(
        self,
        page: dict[str, Any],
        dir_path: str,
        used_keys: set[tuple[str, str]],
    ) -> tuple[str, str]:
        """Return unique (path, filename) using page id when titles collide."""
        page_id = str(page["id"])
        filename = _sanitize_path_segment(page.get("title", "Untitled")) + ".txt"
        key = (dir_path, filename)
        if key in used_keys:
            stem = filename.removesuffix(".txt")
            filename = f"{stem}_{page_id}.txt"
            key = (dir_path, filename)
        used_keys.add(key)
        return dir_path, filename

    def _space_prefixed_path(self, dir_path: str) -> str:
        """Prefix ancestor path with Confluence space key for multi-space KBs."""
        if dir_path:
            return f"{self.space_key}/{dir_path}"
        return self.space_key

    def _add_page_entry(
        self,
        entries: list[ManifestEntry],
        page: dict[str, Any],
        dir_path: str,
        used_keys: set[tuple[str, str]],
    ) -> None:
        page_id = str(page["id"])
        version = page.get("version", {}).get("number", 0)

        checksum = hashlib.sha256(
            f"{page_id}:v{version}".encode()
        ).hexdigest()[:16]

        dir_path = self._space_prefixed_path(dir_path)
        dir_path, filename = self._unique_path_filename(page, dir_path, used_keys)

        entries.append(
            ManifestEntry(
                filename=filename,
                path=dir_path,
                checksum=checksum,
                size=0,
            )
        )

        self._page_cache[(dir_path, filename)] = page_id

    def read_file(self, path: str, filename: str) -> bytes:
        """Fetch a page's content and return as text."""
        page_id = self._page_cache.get((path, filename))
        if not page_id:
            raise FileNotFoundError(f"Page not found: {path}/{filename}" if path else filename)

        if self._api_version == "v2":
            resp = self._http.get(
                f"/pages/{page_id}",
                params={"body-format": "storage"},
            )
        else:
            resp = self._http.get(
                f"/content/{page_id}",
                params={"expand": "body.storage,version"},
            )

        resp.raise_for_status()
        data = resp.json()

        storage = data.get("body", {}).get("storage", {}).get("value", "")
        title = data.get("title", "")
        text = _storage_to_text(storage, title=title)
        text = _finalize_page_text(
            text,
            title=title,
            page_id=page_id,
            space_key=self.space_key,
            base_url=self._base_url,
        )
        return text.encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_confluence_source(source: str) -> dict[str, str | None]:
    """Parse a confluence:SPACEKEY source string.

    Examples:
        confluence:ENG
        confluence:https://company.atlassian.net/ENG
    """
    source = source.removeprefix("confluence:")

    # Check if it includes a URL.
    if source.startswith("https://"):
        parts = source.rsplit("/", 1)
        if len(parts) == 2:
            return {"base_url": parts[0], "space_key": parts[1]}
        raise ValueError("Invalid Confluence source. Expected: confluence:SPACEKEY")

    return {"space_key": source, "base_url": None}
