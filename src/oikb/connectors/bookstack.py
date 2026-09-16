# ---
# co_authored_by:
#   - @hexsm
#     pull_request: https://github.com/open-webui/oikb/pull/84
# ---

"""BookStack connector — sync pages, chapters, or books from a BookStack instance.

Auth via BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET env vars.
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import parse_qsl

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


FORMAT_EXPORT = {"txt": "plaintext", "md": "markdown", "html": "html", "pdf": "pdf"}
FORMAT_EXT = {"txt": ".txt", "md": ".md", "html": ".html", "pdf": ".pdf"}
SCOPES = {"pages", "books", "shelves"}
OUTPUTS = {"pages", "chapters", "books"}
STRUCTURES = {"flat", "hierarchical"}
PARAMS = {"include_ids", "exclude_ids", "output", "format", "structure"}


class BookStackConnector(BaseConnector):
    """Sync pages from BookStack."""

    def __init__(
        self,
        base_url: str | None = None,
        token_id: str | None = None,
        token_secret: str | None = None,
        include_ids: list[str] | None = None,
        exclude_ids: list[str] | None = None,
        scope: str = "books",
        export_output: str = "pages",
        export_format: str = "md",
        structure: str = "flat",
    ):
        self._url = (base_url or os.environ.get("BOOKSTACK_URL", "")).rstrip("/")
        tid = token_id or os.environ.get("BOOKSTACK_TOKEN_ID", "")
        ts = token_secret or os.environ.get("BOOKSTACK_TOKEN_SECRET", "")
        if not self._url or not tid or not ts:
            raise ValueError("BookStack credentials required. Set BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET.")
        self._http = httpx.Client(base_url=self._url, headers={"Authorization": f"Token {tid}:{ts}"}, timeout=30.0)
        self.include_ids = include_ids or []
        self.exclude_ids = set(exclude_ids or [])
        self.scope = scope
        self.export_output = export_output
        self.export_format = export_format
        self.structure = structure
        self._cache: dict[str, tuple[str, str]] = {}
        self._books: dict[str, dict] = {}
        self._chapters: dict[str, dict] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        if self.scope == "pages":
            entries = self._page_entries(self._selected_pages())
        elif self.scope == "shelves":
            entries = self._shelf_entries()
        else:
            entries = self._book_scope_entries(self._selected_books())
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _book_scope_entries(self, books: list[dict], shelf_name: str | None = None) -> list[ManifestEntry]:
        if self.export_output == "books":
            return [self._book_entry(self._get_book(str(book["id"])), shelf_name) for book in books]
        if self.export_output == "chapters":
            entries: list[ManifestEntry] = []
            for book in books:
                full_book = self._get_book(str(book["id"]))
                for item in full_book.get("contents", []) or []:
                    if item.get("type") == "chapter" and item.get("pages"):
                        entries.append(self._chapter_entry(item, full_book, shelf_name))
                    elif item.get("type") == "page":
                        entries.append(self._page_entry(item, shelf_name))
            return entries

        entries: list[ManifestEntry] = []
        for book in books:
            book_id = str(book["id"])
            self._books[book_id] = book
            entries.extend(self._page_entries(self._list_paginated("/api/pages", {"filter[book_id]": book_id}), shelf_name))
        return entries

    def _shelf_entries(self) -> list[ManifestEntry]:
        shelves = [self._get_shelf(shelf_id) for shelf_id in self.include_ids] if self.include_ids else [
            self._get_shelf(str(shelf["id"])) for shelf in self._list_paginated("/api/shelves")
        ]
        entries: list[ManifestEntry] = []
        seen_books: set[str] = set()
        for shelf in shelves:
            if self._excluded(shelf):
                continue
            books = []
            for book in shelf.get("books", []):
                book_id = str(book["id"])
                if book_id not in seen_books:
                    seen_books.add(book_id)
                    books.append(book)
            shelf_name = shelf.get("name", "Untitled") if self.structure == "hierarchical" else None
            entries.extend(self._book_scope_entries(books, shelf_name))
        return entries

    def _selected_pages(self) -> list[dict]:
        pages = [self._get_page(page_id) for page_id in self.include_ids] if self.include_ids else self._list_paginated("/api/pages")
        return [page for page in pages if not self._excluded(page)]

    def _selected_books(self) -> list[dict]:
        books = [self._get_book(book_id) for book_id in self.include_ids] if self.include_ids else self._list_paginated("/api/books")
        return [book for book in books if not self._excluded(book)]

    def _page_entries(self, pages: list[dict], shelf_name: str | None = None) -> list[ManifestEntry]:
        return [self._page_entry(page, shelf_name) for page in pages if not self._excluded(page)]

    def _page_entry(self, page: dict, shelf_name: str | None = None) -> ManifestEntry:
        page_id = str(page["id"])
        filename = self._filename(page_id, page.get("name"), self.export_format)
        path = self._page_path(page, shelf_name)
        checksum = self._checksum("page", page_id, str(page.get("updated_at", "")))
        self._cache[self._entry_key(path, filename)] = ("pages", page_id)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _book_entry(self, book: dict, shelf_name: str | None = None) -> ManifestEntry:
        book_id = str(book["id"])
        filename = self._filename(book_id, book.get("name"), self.export_format)
        path = self._safe_name(shelf_name) if shelf_name and self.structure == "hierarchical" else ""
        updates = [str(book.get("updated_at", ""))]
        for item in book.get("contents", []) or []:
            updates.append(str(item.get("updated_at", "")))
            updates.extend(str(page.get("updated_at", "")) for page in item.get("pages", []) or [])
        checksum = self._checksum("book", book_id, ":".join(updates))
        self._cache[self._entry_key(path, filename)] = ("books", book_id)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _chapter_entry(self, chapter: dict, book: dict, shelf_name: str | None = None) -> ManifestEntry:
        chapter_id = str(chapter["id"])
        filename = self._filename(chapter_id, chapter.get("name"), self.export_format)
        path = self._chapter_path(book, shelf_name)
        updates = [str(chapter.get("updated_at", ""))]
        updates.extend(str(page.get("updated_at", "")) for page in chapter.get("pages", []) or [])
        checksum = self._checksum("chapter", chapter_id, ":".join(updates))
        self._cache[self._entry_key(path, filename)] = ("chapters", chapter_id)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _page_path(self, page: dict, shelf_name: str | None = None) -> str:
        if self.structure != "hierarchical":
            return ""
        parts = [shelf_name] if shelf_name else []
        if page.get("book_id"):
            parts.append(self._get_book(str(page["book_id"])).get("name", "Untitled"))
        if page.get("chapter_id"):
            parts.append(self._get_chapter(str(page["chapter_id"])).get("name", "Untitled"))
        return "/".join(self._safe_name(part) for part in parts if part)

    def _chapter_path(self, book: dict, shelf_name: str | None = None) -> str:
        if self.structure != "hierarchical":
            return ""
        parts = [shelf_name, book.get("name", "Untitled")] if shelf_name else [book.get("name", "Untitled")]
        return "/".join(self._safe_name(part) for part in parts if part)

    def _list_paginated(self, endpoint: str, params: dict[str, str] | None = None) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            page_params: dict[str, str | int] = {"count": 100, "offset": offset}
            if params:
                page_params.update(params)
            resp = self._http.get(endpoint, params=page_params)
            resp.raise_for_status()
            page = resp.json().get("data", [])
            items.extend(page)
            if len(page) < 100:
                break
            offset += 100
        return items

    def read_file(self, path: str, filename: str) -> bytes:
        cached = self._cache.get(self._entry_key(path, filename))
        if not cached:
            raise FileNotFoundError(f"BookStack entry not found: {self._entry_key(path, filename)}")
        resource, item_id = cached
        resp = self._http.get(f"/api/{resource}/{item_id}/export/{FORMAT_EXPORT[self.export_format]}")
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._http.close()

    def _get_page(self, page_id: str) -> dict:
        resp = self._http.get(f"/api/pages/{page_id}")
        resp.raise_for_status()
        return resp.json()

    def _get_book(self, book_id: str) -> dict:
        if book_id not in self._books:
            resp = self._http.get(f"/api/books/{book_id}")
            resp.raise_for_status()
            self._books[book_id] = resp.json()
        return self._books[book_id]

    def _get_chapter(self, chapter_id: str) -> dict:
        if chapter_id not in self._chapters:
            resp = self._http.get(f"/api/chapters/{chapter_id}")
            resp.raise_for_status()
            self._chapters[chapter_id] = resp.json()
        return self._chapters[chapter_id]

    def _get_shelf(self, shelf_id: str) -> dict:
        resp = self._http.get(f"/api/shelves/{shelf_id}")
        resp.raise_for_status()
        return resp.json()

    def _checksum(self, entity: str, item_id: str, updated_at: str) -> str:
        value = f"{entity}:{item_id}:{updated_at}:{self.scope}:{self.export_output}:{self.export_format}:{self.structure}"
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def _excluded(self, item: dict) -> bool:
        return str(item["id"]) in self.exclude_ids

    @classmethod
    def _filename(cls, item_id: str, name: str | None, fmt: str) -> str:
        return f"{item_id}_{cls._safe_name(name)[:80]}{FORMAT_EXT[fmt]}"

    @staticmethod
    def _safe_name(name: str | None) -> str:
        safe = re.sub(r'[<>:"/\\|?*]', "_", name or "Untitled").strip()
        return safe or "Untitled"

    @staticmethod
    def _entry_key(path: str, filename: str) -> str:
        return f"{path}/{filename}" if path else filename


def parse_bookstack_source(source: str) -> dict:
    raw = source.removeprefix("bookstack:")
    scope, sep, query = raw.partition("?")
    result = {"include_ids": [], "exclude_ids": [], "scope": scope or "books", "output": "pages", "format": "md", "structure": "flat"}
    seen: set[str] = set()

    for key, value in parse_qsl(query, keep_blank_values=True) if sep else []:
        if key in seen:
            raise ValueError(f"Invalid BookStack source. Duplicate parameter: {key}")
        seen.add(key)
        if key not in PARAMS:
            raise ValueError(f"Invalid BookStack source. Unknown parameter: {key}")
        result[key] = _parse_ids(value) if key in {"include_ids", "exclude_ids"} else value

    if result["scope"] not in SCOPES:
        raise ValueError("Invalid BookStack source. Expected bookstack:, bookstack:pages, bookstack:books, or bookstack:shelves")
    if result["include_ids"] and result["exclude_ids"]:
        raise ValueError("Invalid BookStack source. include_ids and exclude_ids cannot be used together")
    if result["output"] not in OUTPUTS:
        raise ValueError("Invalid BookStack source. Expected output=pages, output=chapters, or output=books")
    if result["format"] not in FORMAT_EXPORT:
        raise ValueError("Invalid BookStack source. Expected format=txt, format=md, format=html, or format=pdf")
    if result["structure"] not in STRUCTURES:
        raise ValueError("Invalid BookStack source. Expected structure=flat or structure=hierarchical")
    if result["scope"] == "pages":
        result["output"] = "pages"
    return result


def _parse_ids(raw: str) -> list[str]:
    parts = raw.split(",")
    if any(not part or not part.isdecimal() or int(part) <= 0 for part in parts):
        raise ValueError("Invalid BookStack source. Expected positive numeric IDs, e.g. include_ids=12,34")
    return list(dict.fromkeys(parts))
