"""Confluence connector — sync a Confluence space to a Knowledge Base.

Supports Cloud REST API v2 (default) and Server/Data Center REST API v1.
Set CONFLUENCE_API_VERSION=v1 for Server/Data Center. Authentication uses
Basic auth with CONFLUENCE_USER, or Bearer auth when only a token is supplied.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
from collections import Counter
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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
    """Sync pages from a Confluence space.

    Args:
        space_key: Confluence space key (e.g. "ENG").
        base_url:  Confluence instance URL (or CONFLUENCE_URL env var).
        user:      Confluence user email (or CONFLUENCE_USER env var).
        token:     Confluence API token (or CONFLUENCE_TOKEN env var).
        structure: "flat" or "hierarchical" manifest paths.
        api_version: "v1" or "v2" (or CONFLUENCE_API_VERSION, default "v2").
        include_source_url: prepend the original page link to extracted text.
    """

    def __init__(
        self,
        space_key: str,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
        structure: str = "flat",
        api_version: str | None = None,
        include_source_url: bool = False,
    ):
        if structure not in {"flat", "hierarchical"}:
            raise ValueError("structure must be 'flat' or 'hierarchical'")
        self.space_key = space_key
        self.structure = structure
        self._api_version = (api_version or os.environ.get("CONFLUENCE_API_VERSION", "v2")).lower()
        if self._api_version not in {"v1", "v2"}:
            raise ValueError("CONFLUENCE_API_VERSION must be 'v1' or 'v2'")

        self._base_url = (base_url or os.environ.get("CONFLUENCE_URL", "")).rstrip("/")
        self._user = user if user is not None else os.environ.get("CONFLUENCE_USER", "")
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

        headers = {"Accept": "application/json"}
        if not self._user:
            headers["Authorization"] = f"Bearer {self._token}"
        api_base = self._base_url
        if self._api_version == "v2" and not api_base.endswith("/wiki"):
            api_base += "/wiki"
        self._source_base = None
        if include_source_url:
            # Only expose the configured instance and a numeric page ID. Do
            # not put upstream _links URLs or authentication data into the KB.
            parts = urlsplit(self._base_url)
            if (
                parts.scheme not in {"http", "https"}
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or "?" in self._base_url
                or "#" in self._base_url
                or any(c.isspace() or c in "<>\\" or ord(c) < 32 for c in self._base_url)
            ):
                raise ValueError(
                    "include-source-url requires an HTTP(S) Confluence instance URL "
                    "without credentials, query or fragment"
                )
            self._source_base = api_base
        self._http = httpx.Client(
            base_url=api_base,
            auth=(self._user, self._token) if self._user else None,
            headers=headers,
            timeout=60.0,
        )

        # Resolve space key to numeric ID (v2 API requires ID).
        if self._api_version == "v2" and not self.space_key.isdecimal():
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
        params: dict[str, Any] = {"limit": 250}
        endpoint = f"/api/v2/spaces/{self.space_key}/pages"
        if self._api_version == "v1":
            endpoint = "/rest/api/content"
            params.update(spaceKey=self.space_key, type="page", start=0, expand="ancestors,version")
        seen_pages: set[str] = set()

        while True:
            resp = self._http.get(endpoint, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data["results"]
            for page in results:
                page_id = str(page["id"])
                if page_id in seen_pages:
                    raise ValueError(f"Repeated Confluence page in pagination: {page_id}")
                seen_pages.add(page_id)
            pages.extend(results)

            # Handle pagination.
            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            # Use only pagination parameters, never a server-provided host/path.
            parameter = "start" if self._api_version == "v1" else "cursor"
            next_value = dict(parse_qsl(urlsplit(next_link).query)).get(parameter)
            if not results or not next_value or str(params.get(parameter)) == next_value:
                raise ValueError("Invalid Confluence pagination link")
            if parameter == "start" and int(next_value) <= int(params["start"]):
                raise ValueError("Confluence pagination did not advance")
            params[parameter] = next_value

        pages_by_id = {str(page["id"]): page for page in pages}
        entries = [self._page_entry(page, pages_by_id) for page in pages]
        counts = Counter(entry.display_path for entry in entries)
        reserved = set(counts)
        for index, (page, entry) in enumerate(zip(pages, entries)):
            if counts[entry.display_path] > 1:
                # Rename every colliding title so API ordering cannot change identity.
                stem = entry.filename.removesuffix(".txt") + f"_{page['id']}"
                candidate = replace(entry, filename=f"{stem}.txt")
                while candidate.display_path in reserved:
                    stem += "_"
                    candidate = replace(entry, filename=f"{stem}.txt")
                entry = entries[index] = candidate
                reserved.add(entry.display_path)
            self._page_cache[entry.display_path] = str(page["id"])
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _page_entry(
        self, page: dict[str, Any], pages_by_id: dict[str, dict[str, Any]]
    ) -> ManifestEntry:
        page_id = str(page["id"])
        title = page["title"]
        version = page.get("version", {}).get("number", 0)
        fingerprint = f"{page_id}:v{version}"
        if source_url := self._source_url(page_id):
            # Reupload when the emitted link changes, even at the same page
            # version. With no link, preserve the previous default checksum.
            fingerprint += f"\0source-url-v1\0{source_url}"
        checksum = hashlib.sha256(fingerprint.encode()).hexdigest()[:16]
        filename = self._safe_name(title) + ".txt"
        path = self._page_path(page, pages_by_id)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _source_url(self, page_id: str) -> str | None:
        if self._source_base and re.fullmatch(r"[0-9]+", page_id):
            return f"{self._source_base}/pages/viewpage.action?pageId={page_id}"
        return None

    def _page_path(
        self, page: dict[str, Any], pages_by_id: dict[str, dict[str, Any]]
    ) -> str:
        if self.structure != "hierarchical":
            return ""
        if self._api_version == "v1":
            return "/".join(self._safe_name(a.get("title")) for a in page.get("ancestors", []))

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

        if self._api_version == "v1":
            resp = self._http.get(f"/rest/api/content/{page_id}", params={"expand": "body.storage"})
        else:
            resp = self._http.get(f"/api/v2/pages/{page_id}", params={"body-format": "storage"})
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
        if source_url := self._source_url(page_id):
            text = f"Source: {source_url}\n\n{text}"
        return text.encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_confluence_source(source: str) -> dict[str, str | None]:
    """Parse a confluence:SPACEKEY source string.

    Examples:
        confluence:ENG
        confluence:https://company.atlassian.net/ENG
        confluence:ENG?structure=hierarchical
        confluence:ENG?include-source-url=true
    """
    source = source.removeprefix("confluence:")
    is_url = source.startswith(("http://", "https://"))
    parsed = urlsplit(source if is_url else f"confluence://{source}")
    base_path = ""
    space_key = parsed.netloc
    if is_url:
        base_path, _, space_key = parsed.path.rstrip("/").rpartition("/")
    if not space_key or (not is_url and parsed.path):
        raise ValueError("Invalid Confluence source. Expected: confluence:SPACEKEY")

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    unknown = set(params) - {"structure", "include-source-url"}
    if unknown:
        raise ValueError(f"Invalid Confluence source. Unknown parameter: {min(unknown)}")
    structure = params.get("structure", "flat")
    if structure not in {"flat", "hierarchical"}:
        raise ValueError("Invalid Confluence source. Expected structure=flat or structure=hierarchical")

    options = {
        "base_url": f"{parsed.scheme}://{parsed.netloc}{base_path}" if is_url else None,
        "space_key": space_key,
        "structure": structure,
    }
    if "include-source-url" in params:
        if params["include-source-url"] not in {"true", "false"}:
            raise ValueError(
                "Invalid Confluence source. Expected include-source-url=true or include-source-url=false"
            )
        options["include_source_url"] = params["include-source-url"]
    return options
