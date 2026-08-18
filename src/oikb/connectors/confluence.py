"""Confluence connector — sync a Confluence space to a Knowledge Base.

Uses the Confluence Cloud REST API v2. Pages are exported as plain text.
Auth via CONFLUENCE_URL, CONFLUENCE_USER, CONFLUENCE_TOKEN env vars.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


def _storage_to_text(storage_html: str) -> str:
    """Convert Confluence storage format (XHTML) to plain text."""
    # Strip all HTML tags.
    text = re.sub(r"<[^>]+>", " ", storage_html)
    text = html.unescape(text)
    # Collapse whitespace.
    text = re.sub(r"\s+", " ", text).strip()
    return text


class ConfluenceConnector(BaseConnector):
    """Sync pages from a Confluence Cloud space.

    Args:
        space_key: Confluence space key (e.g. "ENG").
        base_url:  Confluence instance URL (or CONFLUENCE_URL env var).
        user:      Confluence user email (or CONFLUENCE_USER env var).
        token:     Confluence API token (or CONFLUENCE_TOKEN env var).
        structure: "flat" or "hierarchical" manifest paths.
    """

    def __init__(
        self,
        space_key: str,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
        structure: str = "flat",
    ):
        if structure not in {"flat", "hierarchical"}:
            raise ValueError("structure must be 'flat' or 'hierarchical'")
        self.space_key = space_key
        self.structure = structure

        self._base_url = (base_url or os.environ.get("CONFLUENCE_URL", "")).rstrip("/")
        self._user = user or os.environ.get("CONFLUENCE_USER", "")
        self._token = token or os.environ.get("CONFLUENCE_TOKEN", "")

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

        self._http = httpx.Client(
            base_url=f"{self._base_url}/wiki",
            auth=(self._user, self._token) if self._user else None,
            headers={"Accept": "application/json"},
            timeout=60.0,
        )

        # Resolve space key to numeric ID (v2 API requires ID).
        if not self.space_key.isdecimal():
            try:
                resp = self._http.get(
                    "/api/v2/spaces", params={"keys": [self.space_key]}
                )
                resp.raise_for_status()
                results = resp.json().get("results", [])
                matches = [
                    space
                    for space in results
                    if space.get("key", "").casefold() == self.space_key.casefold()
                ]
                if len(matches) != 1:
                    raise ValueError(f"Confluence space '{self.space_key}' not found")
                self.space_key = str(matches[0]["id"])
            except Exception:
                self._http.close()
                raise

        # Cache page content for read_file.
        self._page_cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        """List all pages in the space and build a manifest."""
        self._page_cache.clear()
        pages: list[dict[str, Any]] = []
        cursor = None

        while True:
            params: dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor

            resp = self._http.get(
                f"/api/v2/spaces/{self.space_key}/pages",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()

            pages.extend(data.get("results", []))

            # Handle pagination.
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            # Extract cursor from next link.
            cursor_match = re.search(r"cursor=([^&]+)", next_link)
            cursor = cursor_match.group(1) if cursor_match else None
            if not cursor:
                break

        pages_by_id = {str(page["id"]): page for page in pages}
        entries = [self._page_entry(page, pages_by_id) for page in pages]
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _page_entry(
        self, page: dict[str, Any], pages_by_id: dict[str, dict[str, Any]]
    ) -> ManifestEntry:
        page_id = page["id"]
        title = page["title"]
        version = page.get("version", {}).get("number", 0)
        checksum = hashlib.sha256(f"{page_id}:v{version}".encode()).hexdigest()[:16]
        filename = self._safe_name(title) + ".txt"
        path = self._page_path(page, pages_by_id)
        cache_key = self._entry_key(path, filename)
        if self.structure == "hierarchical" and cache_key in self._page_cache:
            raise ValueError(f"Duplicate Confluence page path: {cache_key}")
        self._page_cache[cache_key] = page_id
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _page_path(
        self, page: dict[str, Any], pages_by_id: dict[str, dict[str, Any]]
    ) -> str:
        if self.structure != "hierarchical":
            return ""

        ancestors: list[str] = []
        parent_id = page.get("parentId")
        seen: set[str] = set()
        while parent_id:
            parent_id = str(parent_id)
            if parent_id in seen:
                raise ValueError(f"Circular Confluence page hierarchy at page {page['id']}")
            seen.add(parent_id)
            parent = pages_by_id.get(parent_id)
            if not parent:
                break
            ancestors.append(self._safe_name(parent.get("title")))
            parent_id = parent.get("parentId")
        return "/".join(reversed(ancestors))

    @staticmethod
    def _safe_name(name: str | None) -> str:
        safe = re.sub(r'[<>:"/\\|?*]', "_", name or "Untitled").strip()
        return safe or "Untitled"

    @staticmethod
    def _entry_key(path: str, filename: str) -> str:
        return f"{path}/{filename}" if path else filename

    def read_file(self, path: str, filename: str) -> bytes:
        """Fetch a page's content and return as text."""
        page_id = self._page_cache.get(self._entry_key(path, filename))
        if not page_id:
            raise FileNotFoundError(f"Page not found: {filename}")

        resp = self._http.get(
            f"/api/v2/pages/{page_id}",
            params={"body-format": "storage"},
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
        confluence:ENG?structure=hierarchical
    """
    source = source.removeprefix("confluence:")
    is_url = source.startswith(("http://", "https://"))
    parsed = urlsplit(source if is_url else f"confluence://{source}")
    space_key = parsed.path.strip("/") if is_url else parsed.netloc
    if not space_key or "/" in space_key:
        raise ValueError("Invalid Confluence source. Expected: confluence:SPACEKEY")

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    unknown = set(params) - {"structure"}
    if unknown:
        raise ValueError(f"Invalid Confluence source. Unknown parameter: {min(unknown)}")
    structure = params.get("structure", "flat")
    if structure not in {"flat", "hierarchical"}:
        raise ValueError("Invalid Confluence source. Expected structure=flat or structure=hierarchical")

    return {
        "base_url": f"{parsed.scheme}://{parsed.netloc}" if is_url else None,
        "space_key": space_key,
        "structure": structure,
    }
