"""BookStack connector — sync BookStack content.

Auth via BOOKSTACK_URL, BOOKSTACK_TOKEN_ID, BOOKSTACK_TOKEN_SECRET env vars.
"""

from __future__ import annotations

import hashlib
import os
import re
from urllib.parse import parse_qsl

import httpx

from oikb.connectors import BaseConnector, ManifestEntry

FORMAT_EXPORT = {
    "txt": "plaintext",
    "md": "markdown",
    "html": "html",
    "pdf": "pdf",
}

FORMAT_EXT = {
    "txt": ".txt",
    "md": ".md",
    "html": ".html",
    "pdf": ".pdf",
}

SCOPES = {"pages", "books", "shelves"}
OUTPUTS = {"pages", "chapters", "books"}
STRUCTURES = {"flat", "hierarchical"}
PARAMS = {"scope", "output", "format", "structure"}


class BookStackConnector(BaseConnector):
    """Sync pages or books from BookStack using native export endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        token_id: str | None = None,
        token_secret: str | None = None,
        ids: list[str] | None = None,
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
        self.ids = ids or []
        self.scope = scope
        self.export_output = export_output
        self.export_format = export_format
        self.structure = structure
        self._cache: dict[str, tuple[str, str]] = {}
        self._books: dict[str, dict] = {}
        self._chapters: dict[str, dict] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        if self.scope == "pages":
            entries = self._build_pages_scope_manifest()
        elif self.scope == "books" and self.export_output == "books":
            entries = self._build_books_manifest(self._selected_books())
        elif self.scope == "books" and self.export_output == "chapters":
            entries = self._build_chapters_manifest(self._selected_books())
        elif self.scope == "books":
            entries = self._build_pages_from_books(self._selected_books())
        else:
            entries = self._build_shelves_manifest()
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _build_pages_scope_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        if self.ids:
            pages = [self._get_page(page_id) for page_id in self.ids]
        else:
            pages = self._list_paginated("/api/pages")
        for page in pages:
            entries.append(self._page_entry(page))
        return entries

    def _build_pages_from_books(self, books: list[dict], shelf_name: str | None = None) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        for book in books:
            book_id = str(book["id"])
            self._books[book_id] = book
            pages = self._list_paginated("/api/pages", {"filter[book_id]": book_id})
            for page in pages:
                entries.append(self._page_entry(page, shelf_name=shelf_name))
        return entries

    def _build_books_manifest(self, books: list[dict], shelf_name: str | None = None) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        for book in books:
            entries.append(self._book_entry(self._get_book(str(book["id"])), shelf_name=shelf_name))
        return entries

    def _build_chapters_manifest(self, books: list[dict], shelf_name: str | None = None) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        for book in books:
            full_book = self._get_book(str(book["id"]))
            for item in full_book.get("contents", []) or []:
                if item.get("type") == "chapter" and item.get("pages"):
                    entries.append(self._chapter_entry(item, full_book, shelf_name=shelf_name))
                elif item.get("type") == "page":
                    entries.append(self._page_entry(item, shelf_name=shelf_name))
        return entries

    def _build_shelves_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        if self.ids:
            shelves = [self._get_shelf(shelf_id) for shelf_id in self.ids]
        else:
            shelves = [self._get_shelf(str(shelf["id"])) for shelf in self._list_paginated("/api/shelves")]
        # A book can appear on multiple shelves; export it only once.
        seen_books: set[str] = set()
        for shelf in shelves:
            shelf_name = shelf.get("name", "Untitled") if self.structure == "hierarchical" else None
            books = []
            for book in shelf.get("books", []):
                book_id = str(book["id"])
                if book_id in seen_books:
                    continue
                seen_books.add(book_id)
                books.append(book)
            if self.export_output == "books":
                entries.extend(self._build_books_manifest(books, shelf_name=shelf_name))
            elif self.export_output == "chapters":
                entries.extend(self._build_chapters_manifest(books, shelf_name=shelf_name))
            else:
                entries.extend(self._build_pages_from_books(books, shelf_name=shelf_name))
        return entries

    def _selected_books(self) -> list[dict]:
        if self.ids:
            return [self._get_book(book_id) for book_id in self.ids]
        return self._list_paginated("/api/books")

    def _page_entry(self, page: dict, shelf_name: str | None = None) -> ManifestEntry:
        page_id = str(page["id"])
        title = self._safe_name(page.get("name", "Untitled"))
        filename = f"{page_id}_{title[:80]}{FORMAT_EXT[self.export_format]}"
        path = self._page_path(page, shelf_name=shelf_name)
        checksum = self._checksum("page", page_id, str(page.get("updated_at") or ""))
        self._cache[self._entry_key(path, filename)] = ("pages", page_id)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _book_entry(self, book: dict, shelf_name: str | None = None) -> ManifestEntry:
        book_id = str(book["id"])
        title = self._safe_name(book.get("name", "Untitled"))
        filename = f"{book_id}_{title[:80]}{FORMAT_EXT[self.export_format]}"
        path = self._safe_name(shelf_name) if shelf_name and self.structure == "hierarchical" else ""
        checksum = self._book_checksum(book)
        self._cache[self._entry_key(path, filename)] = ("books", book_id)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _chapter_entry(self, chapter: dict, book: dict, shelf_name: str | None = None) -> ManifestEntry:
        chapter_id = str(chapter["id"])
        title = self._safe_name(chapter.get("name", "Untitled"))
        filename = f"{chapter_id}_{title[:80]}{FORMAT_EXT[self.export_format]}"
        path = self._chapter_path(book, shelf_name=shelf_name)
        checksum = self._chapter_checksum(chapter)
        self._cache[self._entry_key(path, filename)] = ("chapters", chapter_id)
        return ManifestEntry(filename=filename, path=path, checksum=checksum, size=0)

    def _page_path(self, page: dict, shelf_name: str | None = None) -> str:
        if self.structure != "hierarchical":
            return ""
        parts: list[str] = []
        if shelf_name:
            parts.append(shelf_name)
        book_id = page.get("book_id")
        if book_id:
            parts.append(self._get_book(str(book_id)).get("name", "Untitled"))
        chapter_id = page.get("chapter_id")
        if chapter_id:
            parts.append(self._get_chapter(str(chapter_id)).get("name", "Untitled"))
        return "/".join(self._safe_name(part) for part in parts if part)

    def _chapter_path(self, book: dict, shelf_name: str | None = None) -> str:
        if self.structure != "hierarchical":
            return ""
        parts = []
        if shelf_name:
            parts.append(shelf_name)
        parts.append(book.get("name", "Untitled"))
        return "/".join(self._safe_name(part) for part in parts if part)

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

    def _list_paginated(self, endpoint: str, params: dict[str, str] | None = None) -> list[dict]:
        items: list[dict] = []
        offset = 0
        while True:
            page_params: dict[str, str | int] = {"count": 100, "offset": offset}
            if params:
                page_params.update(params)
            resp = self._http.get(endpoint, params=page_params)
            resp.raise_for_status()
            data = resp.json().get("data", [])
            items.extend(data)
            if len(data) < 100:
                break
            offset += 100
        return items

    def read_file(self, path: str, filename: str) -> bytes:
        key = self._entry_key(path, filename)
        # Filenames are not parsed because titles can change; manifest building records the export target.
        cached = self._cache.get(key)
        if not cached:
            raise FileNotFoundError(f"BookStack entry not found: {key}")
        api_resource, item_id = cached
        resp = self._http.get(f"/api/{api_resource}/{item_id}/export/{FORMAT_EXPORT[self.export_format]}")
        resp.raise_for_status()
        return resp.content

    def close(self) -> None:
        self._http.close()

    def _checksum(self, entity: str, item_id: str, updated_at: str) -> str:
        value = f"{entity}:{item_id}:{updated_at}:{self.scope}:{self.export_output}:{self.export_format}:{self.structure}"
        return hashlib.sha256(value.encode()).hexdigest()[:16]

    def _book_checksum(self, book: dict) -> str:
        book_id = str(book["id"])
        # Book exports include nested chapter/page content, so page updates must invalidate the book file.
        updates = [str(book.get("updated_at") or "")]
        for item in book.get("contents", []) or []:
            if item.get("updated_at"):
                updates.append(str(item["updated_at"]))
            for page in item.get("pages", []) or []:
                if page.get("updated_at"):
                    updates.append(str(page["updated_at"]))
        return self._checksum("book", book_id, ":".join(updates))

    def _chapter_checksum(self, chapter: dict) -> str:
        chapter_id = str(chapter["id"])
        updates = [str(chapter.get("updated_at") or "")]
        for page in chapter.get("pages", []) or []:
            if page.get("updated_at"):
                updates.append(str(page["updated_at"]))
        return self._checksum("chapter", chapter_id, ":".join(updates))

    @staticmethod
    def _safe_name(name: str | None) -> str:
        safe = re.sub(r'[<>:"/\\|?*]', "_", name or "Untitled").strip()
        return safe or "Untitled"

    @staticmethod
    def _entry_key(path: str, filename: str) -> str:
        return f"{path}/{filename}" if path else filename


def parse_bookstack_source(source: str) -> dict:
    raw = source.removeprefix("bookstack:")
    id_part, sep, query = raw.partition("?")
    result = {
        "ids": _parse_ids(id_part),
        "scope": "books",
        "output": "pages",
        "format": "md",
        "structure": "flat",
    }

    seen: set[str] = set()
    for key, value in parse_qsl(query, keep_blank_values=True) if sep else []:
        if key in seen:
            raise ValueError(f"Invalid BookStack source. Duplicate parameter: {key}")
        seen.add(key)
        if key not in PARAMS:
            raise ValueError(f"Invalid BookStack source. Unknown parameter: {key}")
        result[key] = value

    if result["scope"] not in SCOPES:
        raise ValueError("Invalid BookStack source. Expected scope=pages, scope=books, or scope=shelves")
    if result["output"] not in OUTPUTS:
        raise ValueError("Invalid BookStack source. Expected output=pages, output=chapters, or output=books")
    if result["format"] not in FORMAT_EXPORT:
        raise ValueError("Invalid BookStack source. Expected format=txt, format=md, format=html, or format=pdf")
    if result["structure"] not in STRUCTURES:
        raise ValueError("Invalid BookStack source. Expected structure=flat or structure=hierarchical")

    if result["scope"] == "pages" and result["output"] in {"books", "chapters"}:
        result["output"] = "pages"

    return result


def _parse_ids(raw: str) -> list[str]:
    if not raw:
        return []
    parts = raw.split(",")
    if any(not part or not part.isdecimal() or int(part) <= 0 for part in parts):
        raise ValueError("Invalid BookStack source. Expected positive numeric IDs, e.g. bookstack:12,34")
    return list(dict.fromkeys(parts))
