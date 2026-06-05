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


def _parse_api_version(api_version: str | None) -> str:
    version = (api_version or os.environ.get("CONFLUENCE_API_VERSION", "v2")).lower()
    if version not in BASE_ENDPOINTS:
        valid = ", ".join(sorted(BASE_ENDPOINTS))
        raise ValueError(
            f"Invalid Confluence API version {version!r}. "
            f"Expected one of: {valid}. Set CONFLUENCE_API_VERSION=v1 or v2."
        )
    return version


def _storage_to_text(storage_html: str) -> str:
    """Convert Confluence storage format (XHTML) to plain text."""
    # Strip all HTML tags.
    text = re.sub(r"<[^>]+>", " ", storage_html)
    text = html.unescape(text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


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

        # Cache page content for read_file.
        self._page_cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        """List all pages in the space and build a manifest."""
        if self._api_version == "v2":
            return self._build_manifest_v2()
        return self._build_manifest_v1()

    def _build_manifest_v1(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        start = 0
        limit = 250

        while True:
            params: dict[str, Any] = {
                "spaceKey": self.space_key,
                "type": "page",
                "limit": limit,
                "start": start,
            }

            resp = self._http.get("/content", params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data.get("results", [])
            if not results:
                break

            for page in results:
                self._add_page_entry(entries, page)

            if len(results) < limit:
                break
            start += len(results)

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _build_manifest_v2(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
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

            for page in data.get("results", []):
                self._add_page_entry(entries, page)

            # Handle pagination.
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            # Extract cursor from next link.
            cursor_match = re.search(r"cursor=([^&]+)", next_link)
            cursor = cursor_match.group(1) if cursor_match else None
            if not cursor:
                break

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _add_page_entry(
        self, entries: list[ManifestEntry], page: dict[str, Any]
    ) -> None:
        page_id = page["id"]
        title = page["title"]
        version = page.get("version", {}).get("number", 0)

        # Use version number as part of checksum.
        checksum = hashlib.sha256(
            f"{page_id}:v{version}".encode()
        ).hexdigest()[:16]

        # Sanitize title for filename.
        filename = re.sub(r'[<>:"/\\|?*]', "_", title) + ".txt"

        entries.append(
            ManifestEntry(
                filename=filename,
                path="",
                checksum=checksum,
                size=0,
            )
        )

        # Store page ID for later retrieval.
        self._page_cache[filename] = page_id

    def read_file(self, path: str, filename: str) -> bytes:
        """Fetch a page's content and return as text."""
        page_id = self._page_cache.get(filename)
        if not page_id:
            raise FileNotFoundError(f"Page not found: {filename}")

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
        text = _storage_to_text(storage)
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
