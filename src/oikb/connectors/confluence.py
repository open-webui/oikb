"""Confluence connector — sync a Confluence space (or every space) to a Knowledge Base.

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

# Sentinel space_key meaning "every space the credentials can see", instead of
# one named space. Confluence space keys cannot contain "*", so this can never
# collide with a real key.
_ALL_SPACES = "*"

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
    """Sync pages and blog posts from a Confluence space — or every space —
    to a Knowledge Base.

    Blog posts are always synced alongside pages, but never mixed into the
    page structure: they get their own "blogposts/YYYY/MM/" subfolder (by
    creation date), mirroring how Confluence organizes its own blog archive,
    since posts have no page hierarchy of their own to place them by.

    Args:
        space_key: Confluence space key (e.g. "ENG"), or "*" to sync every
            space the credentials can see. When syncing every space, each
            space's content is placed under a subfolder named after that
            space's key, in addition to whatever `structure` adds within it.
        base_url:  Confluence instance URL (or CONFLUENCE_URL env var).
        user:      Confluence user email (or CONFLUENCE_USER env var).
        token:     Confluence API token (or CONFLUENCE_TOKEN env var).
        structure: "flat" or "hierarchical" manifest paths for pages within
            a space. Does not affect blog posts, which are always filed
            under their own dated "blogposts/" subfolder.
        api_version: "v1" or "v2" (or CONFLUENCE_API_VERSION, default "v2").
    """

    def __init__(
        self,
        space_key: str,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
        structure: str = "flat",
        api_version: str | None = None,
    ):
        if structure not in {"flat", "hierarchical"}:
            raise ValueError("structure must be 'flat' or 'hierarchical'")
        if not space_key:
            raise ValueError("space_key is required (a space key, or '*' for every space)")
        self.space_key = space_key
        self.structure = structure
        self._all_spaces = space_key == _ALL_SPACES
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
        self._http = httpx.Client(
            base_url=api_base,
            auth=(self._user, self._token) if self._user else None,
            headers=headers,
            timeout=60.0,
        )

        # Resolve space key to numeric ID (v2 API requires ID). Not needed
        # when syncing every space: each space's own ID is read straight off
        # the space-listing response in _list_spaces().
        if self._api_version == "v2" and not self._all_spaces and not self.space_key.isdecimal():
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

        # Cache page/blogpost content for read_file, keyed by manifest
        # display_path -> (content id, "page" | "blogpost"). The type is
        # needed because v2 has separate single-item endpoints per type.
        self._page_cache: dict[str, tuple[str, str]] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        """List all pages and blog posts (in the space, or every space)."""
        self._page_cache.clear()
        entries: list[ManifestEntry] = []

        for query_key, folder in self._list_spaces():
            pages = self._fetch_content(query_key, "page")
            blogposts = self._fetch_content(query_key, "blogpost")
            pages_by_id = {str(page["id"]): page for page in pages}

            space_items: list[tuple[dict[str, Any], str]] = [
                (page, "page") for page in pages
            ] + [(post, "blogpost") for post in blogposts]
            space_entries = [
                self._content_entry(item, pages_by_id, folder, content_type)
                for item, content_type in space_items
            ]

            # Collisions can only happen within the same space folder, so dedupe
            # per space rather than across the whole (possibly multi-space) batch.
            counts = Counter(entry.display_path for entry in space_entries)
            reserved = set(counts)
            for index, ((item, content_type), entry) in enumerate(
                zip(space_items, space_entries)
            ):
                if counts[entry.display_path] > 1:
                    # Rename every colliding title so API ordering cannot change identity.
                    stem = entry.filename.removesuffix(".txt") + f"_{item['id']}"
                    candidate = replace(entry, filename=f"{stem}.txt")
                    while candidate.display_path in reserved:
                        stem += "_"
                        candidate = replace(entry, filename=f"{stem}.txt")
                    entry = space_entries[index] = candidate
                    reserved.add(entry.display_path)
                self._page_cache[entry.display_path] = (str(item["id"]), content_type)

            entries.extend(space_entries)

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _list_spaces(self) -> list[tuple[str, str]]:
        """Return (query_key, folder_name) pairs, one per space to sync.

        query_key is what _fetch_pages() needs to list that space's pages (a
        numeric space ID for v2, a space key for v1). folder_name is the
        subfolder to file that space's pages under — empty when syncing a
        single named space (unchanged, root-level behavior), or the space's
        key when syncing every space.
        """
        if not self._all_spaces:
            return [(self.space_key, "")]

        spaces: list[dict[str, Any]] = []
        if self._api_version == "v1":
            start = 0
            while True:
                resp = self._http.get("/rest/api/space", params={"start": start, "limit": 100})
                resp.raise_for_status()
                data = resp.json()
                results = data.get("results", [])
                spaces.extend(results)
                if not results or data.get("_links", {}).get("next") is None:
                    break
                start += len(results)
            return [(space["key"], self._safe_name(space["key"])) for space in spaces]

        cursor = None
        while True:
            params: dict[str, Any] = {"limit": 250}
            if cursor:
                params["cursor"] = cursor
            resp = self._http.get("/api/v2/spaces", params=params)
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])
            spaces.extend(results)

            next_link = data.get("_links", {}).get("next")
            if not next_link:
                break
            next_value = dict(parse_qsl(urlsplit(next_link).query)).get("cursor")
            if not results or not next_value or cursor == next_value:
                raise ValueError("Invalid Confluence pagination link")
            cursor = next_value

        return [
            (str(space["id"]), self._safe_name(space.get("key", space["id"])))
            for space in spaces
        ]

    def _fetch_content(self, space_query_key: str, content_type: str) -> list[dict[str, Any]]:
        """List all pages or blog posts in one space.

        space_query_key is a numeric space ID for v2, a space key for v1.
        content_type is "page" or "blogpost".
        """
        items: list[dict[str, Any]] = []
        params: dict[str, Any] = {"limit": 250}
        endpoint = f"/api/v2/spaces/{space_query_key}/{content_type}s"
        if self._api_version == "v1":
            endpoint = "/rest/api/content"
            expand = "ancestors,version" if content_type == "page" else "version,history"
            params.update(spaceKey=space_query_key, type=content_type, start=0, expand=expand)
        seen: set[str] = set()

        while True:
            resp = self._http.get(endpoint, params=params)
            resp.raise_for_status()
            data = resp.json()

            results = data["results"]
            for item in results:
                item_id = str(item["id"])
                if item_id in seen:
                    raise ValueError(f"Repeated Confluence {content_type} in pagination: {item_id}")
                seen.add(item_id)
            items.extend(results)

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

        return items

    def _content_entry(
        self,
        item: dict[str, Any],
        pages_by_id: dict[str, dict[str, Any]],
        folder: str,
        content_type: str,
    ) -> ManifestEntry:
        item_id = str(item["id"])
        title = item["title"]
        version = item.get("version", {}).get("number", 0)
        # Content IDs are unique across pages and blog posts, so the checksum
        # format is unchanged from before blog posts existed.
        checksum = hashlib.sha256(f"{item_id}:v{version}".encode()).hexdigest()[:16]
        filename = self._safe_name(title) + ".txt"
        content_path = (
            self._blogpost_folder(item) if content_type == "blogpost" else self._page_path(item, pages_by_id)
        )
        path = self._join_path(folder, content_path)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

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

    def _blogpost_folder(self, item: dict[str, Any]) -> str:
        """Blog posts have no page hierarchy, so file them under
        blogposts/YYYY/MM by creation date — mirroring how Confluence's own
        blog archive is organized — regardless of the `structure` setting.
        """
        date = (
            item.get("history", {}).get("createdDate", "")
            if self._api_version == "v1"
            else item.get("createdAt", "")
        )
        if len(date) >= 7 and date[4] == "-":
            return f"blogposts/{date[:4]}/{date[5:7]}"
        return "blogposts"

    @staticmethod
    def _safe_name(name: str | None) -> str:
        safe = re.sub(r'[<>:"/\\|?*]', "_", name or "Untitled").strip()
        return safe or "Untitled"

    @staticmethod
    def _join_path(*parts: str) -> str:
        return "/".join(p for p in parts if p)

    @staticmethod
    def _entry_key(path: str, filename: str) -> str:
        return f"{path}/{filename}" if path else filename

    def read_file(self, path: str, filename: str) -> bytes:
        """Fetch a page's or blog post's content and return as text."""
        cached = self._page_cache.get(self._entry_key(path, filename))
        if not cached:
            raise FileNotFoundError(f"Page not found: {filename}")
        content_id, content_type = cached

        if self._api_version == "v1":
            resp = self._http.get(f"/rest/api/content/{content_id}", params={"expand": "body.storage"})
        else:
            resp = self._http.get(f"/api/v2/{content_type}s/{content_id}", params={"body-format": "storage"})
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
        confluence:ENG?structure=hierarchical
        confluence:*                                    # every space
        confluence:*?structure=hierarchical              # every space, hierarchical within each
        confluence:https://company.atlassian.net         # every space (host with no path)
    """
    source = source.removeprefix("confluence:")
    is_url = source.startswith(("http://", "https://"))
    parsed = urlsplit(source if is_url else f"confluence://{source}")

    if is_url:
        base_path, _, space_key = parsed.path.rstrip("/").rpartition("/")
        if not space_key:
            # Host only, no space in the path: sync every space on it.
            space_key, base_path = _ALL_SPACES, ""
    else:
        base_path = ""
        space_key = parsed.netloc
        if not space_key or parsed.path:
            raise ValueError("Invalid Confluence source. Expected: confluence:SPACEKEY")

    params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    unknown = set(params) - {"structure"}
    if unknown:
        raise ValueError(f"Invalid Confluence source. Unknown parameter: {min(unknown)}")
    structure = params.get("structure", "flat")
    if structure not in {"flat", "hierarchical"}:
        raise ValueError("Invalid Confluence source. Expected structure=flat or structure=hierarchical")

    return {
        "base_url": f"{parsed.scheme}://{parsed.netloc}{base_path}" if is_url else None,
        "space_key": space_key,
        "structure": structure,
    }
