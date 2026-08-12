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

import httpx

from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable

# A link's visible label is the title of another page in the space, which syncs
# as its own file. Dropped with the link so a section index does not become a
# document that is nothing but titles already in the KB.
_LINK_BODY = re.compile(
    r"<ac:plain-text-link-body>.*?</ac:plain-text-link-body>", re.S | re.I
)
_CDATA = re.compile(r"<!\[CDATA\[(.*?)\]\]>", re.S)
# Where one block ends the next begins on its own line: a code block run
# together with the sentence before it reads as one thought.
_BREAK = re.compile(
    r"<br\s*/?>"
    r"|</(?:p|div|h[1-6]|li|ul|ol|tr|td|th|blockquote|pre|table|section)\s*>"
    r"|</ac:[\w.-]+\s*>",
    re.I,
)
_TAG = re.compile(r"<[^>]+>")
# Marks where a parked macro body goes back. Storage format cannot contain NUL.
_PARKED = re.compile("\x00(\\d+)\x00")
_INLINE_SPACE = re.compile(r"[^\S\n]+")


def _storage_to_text(storage_html: str) -> str:
    """Convert Confluence storage format (XHTML) to plain text."""
    if not storage_html:
        return ""

    text = _LINK_BODY.sub(" ", storage_html)

    # Macro bodies are literal text, so they are parked before the markup passes
    # run: `<[^>]+>` would otherwise treat `<![CDATA[...]]>` as one tag and
    # delete a code block or panel whole, and their entities are not escaped.
    parked: list[str] = []

    def _park(match: re.Match) -> str:
        parked.append(match.group(1))
        return f"\n\x00{len(parked) - 1}\x00\n"

    text = _CDATA.sub(_park, text)
    text = _BREAK.sub("\n", text)
    text = _TAG.sub(" ", text)
    text = html.unescape(text)

    lines = (_INLINE_SPACE.sub(" ", line).strip() for line in text.split("\n"))
    text = "\n".join(line for line in lines if line)

    # Restored after the whitespace pass so a code block keeps its own line
    # breaks and indentation.
    return _PARKED.sub(lambda m: parked[int(m.group(1))].strip(), text).strip()


class ConfluenceConnector(BaseConnector):
    """Sync pages from a Confluence Cloud space.

    Args:
        space_key: Confluence space key (e.g. "ENG").
        base_url:  Confluence instance URL (or CONFLUENCE_URL env var).
        user:      Confluence user email (or CONFLUENCE_USER env var).
        token:     Confluence API token (or CONFLUENCE_TOKEN env var).
    """

    def __init__(
        self,
        space_key: str,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
    ):
        self.space_key = space_key

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

        # Cache page content for read_file.
        self._page_cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        """List all pages in the space and build a manifest."""
        entries: list[ManifestEntry] = []
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

            for page in data.get("results", []):
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

    def read_file(self, path: str, filename: str) -> bytes:
        """Fetch a page's content and return as text."""
        page_id = self._page_cache.get(filename)
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
        if not text:
            # Open WebUI extracts text as part of POST /files/ and answers 400
            # for a file it can get nothing out of, so uploading this would fail
            # the page on every run for as long as it exists.
            raise SourceFileUnavailable(
                "page has no text to sync (blank, or only a macro such as a "
                "children index)"
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
