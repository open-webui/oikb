import pytest
from httpx import Request, Response

from oikb.connectors.bookstack import BookStackConnector, parse_bookstack_source


def test_parse_bookstack_defaults():
    assert parse_bookstack_source("bookstack:") == {
        "ids": [],
        "scope": "books",
        "output": "pages",
        "format": "md",
        "structure": "flat",
    }


def test_parse_bookstack_ids_and_options():
    assert parse_bookstack_source("bookstack:12,34?scope=shelves&output=books&format=pdf&structure=hierarchical") == {
        "ids": ["12", "34"],
        "scope": "shelves",
        "output": "books",
        "format": "pdf",
        "structure": "hierarchical",
    }


def test_parse_bookstack_deduplicates_ids():
    assert parse_bookstack_source("bookstack:12,12,34")["ids"] == ["12", "34"]


def test_parse_bookstack_pages_books_fallback():
    parsed = parse_bookstack_source("bookstack:12?scope=pages&output=books")
    assert parsed["output"] == "pages"


def test_parse_bookstack_accepts_chapter_output():
    parsed = parse_bookstack_source("bookstack:12?scope=books&output=chapters")
    assert parsed["output"] == "chapters"


def test_parse_bookstack_pages_chapters_fallback():
    parsed = parse_bookstack_source("bookstack:12?scope=pages&output=chapters")
    assert parsed["output"] == "pages"


@pytest.mark.parametrize(
    "source",
    [
        "bookstack:abc",
        "bookstack:12,,34",
        "bookstack:?scope=book",
        "bookstack:?output=chapter",
        "bookstack:?format=markdown",
        "bookstack:?format=zip",
        "bookstack:?structure=nested",
        "bookstack:?unknown=value",
        "bookstack:?format=md&format=txt",
    ],
)
def test_parse_bookstack_rejects_invalid_sources(source):
    with pytest.raises(ValueError):
        parse_bookstack_source(source)


def test_default_manifest_lists_books_then_pages_as_markdown():
    http = FakeHTTP({
        "/api/books": Response(200, json={"data": [{"id": 12, "name": "Guide", "updated_at": "2026-01-01"}]}),
        "/api/pages": Response(200, json={"data": [{"id": 99, "book_id": 12, "name": "Install", "updated_at": "2026-01-02"}]}),
        "/api/pages/99/export/markdown": Response(200, content=b"# Install"),
    })

    connector = _connector()
    connector._http = http
    try:
        manifest = connector.build_manifest()
        content = connector.read_file(manifest[0].path, manifest[0].filename)
    finally:
        connector.close()

    assert http.calls[0][0] == "/api/books"
    assert http.calls[1][1]["filter[book_id]"] == "12"
    assert manifest[0].filename == "99_Install.md"
    assert manifest[0].path == ""
    assert content == b"# Install"
    assert http.calls[2][0] == "/api/pages/99/export/markdown"


def test_pages_scope_with_ids_reads_pages_directly_as_plaintext():
    http = FakeHTTP({
        "/api/pages/99": Response(200, json={"id": 99, "name": "Install", "updated_at": "2026-01-02"}),
        "/api/pages/99/export/plaintext": Response(200, content=b"Install"),
    })

    connector = _connector(ids=["99"], scope="pages", export_format="txt")
    connector._http = http
    try:
        manifest = connector.build_manifest()
        content = connector.read_file(manifest[0].path, manifest[0].filename)
    finally:
        connector.close()

    assert manifest[0].filename == "99_Install.txt"
    assert content == b"Install"
    assert http.calls[1][0] == "/api/pages/99/export/plaintext"


def test_books_scope_outputs_books_as_pdf():
    http = FakeHTTP({
        "/api/books/12": Response(200, json={"id": 12, "name": "Guide", "updated_at": "2026-01-01"}),
        "/api/books/12/export/pdf": Response(200, content=b"%PDF"),
    })

    connector = _connector(ids=["12"], export_output="books", export_format="pdf", structure="hierarchical")
    connector._http = http
    try:
        manifest = connector.build_manifest()
        content = connector.read_file(manifest[0].path, manifest[0].filename)
    finally:
        connector.close()

    assert manifest[0].filename == "12_Guide.pdf"
    assert manifest[0].path == ""
    assert content == b"%PDF"
    assert http.calls[1][0] == "/api/books/12/export/pdf"


def test_hierarchical_pages_use_book_and_chapter_paths():
    http = FakeHTTP({
        "/api/books/12": Response(200, json={"id": 12, "name": "User Guide", "updated_at": "2026-01-01"}),
        "/api/pages": Response(200, json={"data": [{"id": 99, "book_id": 12, "chapter_id": 7, "name": "Install", "updated_at": "2026-01-02"}]}),
        "/api/chapters/7": Response(200, json={"id": 7, "name": "Setup"}),
    })

    connector = _connector(ids=["12"], structure="hierarchical")
    connector._http = http
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert manifest[0].path == "User Guide/Setup"


def test_shelves_dedupe_books_and_use_shelf_path_for_book_output():
    http = FakeHTTP({
        "/api/shelves/5": Response(
            200,
            json={
                "id": 5,
                "name": "Knowledge",
                "books": [
                    {"id": 12, "name": "Guide", "updated_at": "2026-01-01"},
                    {"id": 12, "name": "Guide", "updated_at": "2026-01-01"},
                ],
            },
        ),
        "/api/books/12": Response(200, json={"id": 12, "name": "Guide", "updated_at": "2026-01-01"}),
    })

    connector = _connector(ids=["5"], scope="shelves", export_output="books", structure="hierarchical")
    connector._http = http
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert len(manifest) == 1
    assert manifest[0].filename == "12_Guide.md"
    assert manifest[0].path == "Knowledge"


def test_book_output_checksum_includes_nested_page_updates():
    old_book = {
        "id": 12,
        "name": "Guide",
        "updated_at": "2026-01-01",
        "contents": [
            {
                "id": 7,
                "type": "chapter",
                "updated_at": "2026-01-01",
                "pages": [{"id": 99, "updated_at": "2026-01-02"}],
            }
        ],
    }
    new_book = {
        **old_book,
        "contents": [
            {
                "id": 7,
                "type": "chapter",
                "updated_at": "2026-01-01",
                "pages": [{"id": 99, "updated_at": "2026-02-01"}],
            }
        ],
    }

    old_http = FakeHTTP({"/api/books/12": Response(200, json=old_book)})
    new_http = FakeHTTP({"/api/books/12": Response(200, json=new_book)})

    old_connector = _connector(ids=["12"], export_output="books")
    new_connector = _connector(ids=["12"], export_output="books")
    old_connector._http = old_http
    new_connector._http = new_http
    try:
        old_checksum = old_connector.build_manifest()[0].checksum
        new_checksum = new_connector.build_manifest()[0].checksum
    finally:
        old_connector.close()
        new_connector.close()

    assert old_checksum != new_checksum


def test_chapter_output_uses_book_contents_and_keeps_standalone_pages():
    http = FakeHTTP({
        "/api/books/12": Response(
            200,
            json={
                "id": 12,
                "name": "Guide",
                "updated_at": "2026-01-01",
                "contents": [
                    {
                        "id": 7,
                        "type": "chapter",
                        "name": "Setup",
                        "updated_at": "2026-01-02",
                        "pages": [
                            {"id": 99, "updated_at": "2026-01-03"},
                            {"id": 100, "updated_at": "2026-01-04"},
                        ],
                    },
                    {"id": 8, "type": "chapter", "name": "Empty", "pages": []},
                    {
                        "id": 101,
                        "type": "page",
                        "book_id": 12,
                        "chapter_id": None,
                        "name": "Standalone",
                        "updated_at": "2026-01-05",
                    },
                ],
            },
        ),
        "/api/chapters/7/export/markdown": Response(200, content=b"# Setup"),
        "/api/pages/101/export/markdown": Response(200, content=b"# Standalone"),
    })

    connector = _connector(ids=["12"], export_output="chapters")
    connector._http = http
    try:
        manifest = connector.build_manifest()
        contents = {entry.filename: connector.read_file(entry.path, entry.filename) for entry in manifest}
    finally:
        connector.close()

    assert [entry.filename for entry in manifest] == ["101_Standalone.md", "7_Setup.md"]
    assert contents == {"7_Setup.md": b"# Setup", "101_Standalone.md": b"# Standalone"}
    assert ("/api/chapters/7/export/markdown", {}) in http.calls
    assert ("/api/pages/101/export/markdown", {}) in http.calls


def test_shelf_chapter_output_uses_shelf_and_book_path():
    http = FakeHTTP({
        "/api/shelves/5": Response(
            200,
            json={"id": 5, "name": "Knowledge", "books": [{"id": 12, "name": "Guide"}]},
        ),
        "/api/books/12": Response(
            200,
            json={
                "id": 12,
                "name": "Guide",
                "contents": [
                    {"id": 7, "type": "chapter", "name": "Setup", "updated_at": "2026-01-02", "pages": [{"id": 99}]},
                    {
                        "id": 101,
                        "type": "page",
                        "book_id": 12,
                        "chapter_id": None,
                        "name": "Standalone",
                        "updated_at": "2026-01-05",
                    },
                ],
            },
        ),
    })

    connector = _connector(ids=["5"], scope="shelves", export_output="chapters", structure="hierarchical")
    connector._http = http
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert [(entry.path, entry.filename) for entry in manifest] == [
        ("Knowledge/Guide", "101_Standalone.md"),
        ("Knowledge/Guide", "7_Setup.md"),
    ]


def test_chapter_output_checksum_includes_nested_page_updates():
    old_book = {
        "id": 12,
        "name": "Guide",
        "contents": [
            {
                "id": 7,
                "type": "chapter",
                "name": "Setup",
                "updated_at": "2026-01-01",
                "pages": [{"id": 99, "updated_at": "2026-01-02"}],
            }
        ],
    }
    new_book = {
        **old_book,
        "contents": [
            {
                "id": 7,
                "type": "chapter",
                "name": "Setup",
                "updated_at": "2026-01-01",
                "pages": [{"id": 99, "updated_at": "2026-02-01"}],
            }
        ],
    }

    old_http = FakeHTTP({"/api/books/12": Response(200, json=old_book)})
    new_http = FakeHTTP({"/api/books/12": Response(200, json=new_book)})

    old_connector = _connector(ids=["12"], export_output="chapters")
    new_connector = _connector(ids=["12"], export_output="chapters")
    old_connector._http = old_http
    new_connector._http = new_http
    try:
        old_checksum = old_connector.build_manifest()[0].checksum
        new_checksum = new_connector.build_manifest()[0].checksum
    finally:
        old_connector.close()
        new_connector.close()

    assert old_checksum != new_checksum


def test_shelf_without_books_returns_empty_manifest():
    http = FakeHTTP({
        "/api/shelves/5": Response(200, json={"id": 5, "name": "Empty", "books": []}),
    })

    connector = _connector(ids=["5"], scope="shelves", export_output="books")
    connector._http = http
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert manifest == []


def test_shelves_scope_without_ids_lists_shelves():
    http = FakeHTTP({
        "/api/shelves": Response(200, json={"data": [{"id": 5, "name": "Knowledge"}]}),
        "/api/shelves/5": Response(200, json={"id": 5, "name": "Knowledge", "books": []}),
    })

    connector = _connector(scope="shelves")
    connector._http = http
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert manifest == []
    assert http.calls[0] == ("/api/shelves", {"count": 100, "offset": 0})
    assert http.calls[1] == ("/api/shelves/5", {})


def test_shelves_scope_outputs_hierarchical_pages():
    http = FakeHTTP({
        "/api/shelves/5": Response(
            200,
            json={
                "id": 5,
                "name": "Knowledge",
                "books": [{"id": 12, "name": "Guide", "updated_at": "2026-01-01"}],
            },
        ),
        "/api/pages": Response(200, json={"data": [{"id": 99, "book_id": 12, "chapter_id": 7, "name": "Install", "updated_at": "2026-01-02"}]}),
        "/api/chapters/7": Response(200, json={"id": 7, "name": "Setup"}),
    })

    connector = _connector(ids=["5"], scope="shelves", structure="hierarchical")
    connector._http = http
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert manifest[0].filename == "99_Install.md"
    assert manifest[0].path == "Knowledge/Guide/Setup"
    assert http.calls[1] == ("/api/pages", {"count": 100, "offset": 0, "filter[book_id]": "12"})


def test_paginated_book_listing_reads_second_page():
    first_page = [{"id": i, "name": f"Book {i}", "updated_at": "2026-01-01"} for i in range(1, 101)]
    second_page = [{"id": 101, "name": "Book 101", "updated_at": "2026-01-01"}]
    http = FakeHTTP({
        "/api/books": [
            Response(200, json={"data": first_page}),
            Response(200, json={"data": second_page}),
        ],
    })

    connector = _connector(export_output="books")
    connector._http = http
    try:
        books = connector._list_paginated("/api/books")
    finally:
        connector.close()

    assert len(books) == 101
    assert http.calls[0] == ("/api/books", {"count": 100, "offset": 0})
    assert http.calls[1] == ("/api/books", {"count": 100, "offset": 100})


@pytest.mark.parametrize(
    ("fmt", "extension", "export_name"),
    [
        ("txt", ".txt", "plaintext"),
        ("md", ".md", "markdown"),
        ("html", ".html", "html"),
        ("pdf", ".pdf", "pdf"),
    ],
)
def test_format_mapping_for_book_exports(fmt, extension, export_name):
    http = FakeHTTP({
        "/api/books/12": Response(200, json={"id": 12, "name": "Guide", "updated_at": "2026-01-01"}),
        f"/api/books/12/export/{export_name}": Response(200, content=b"content"),
    })

    connector = _connector(ids=["12"], export_output="books", export_format=fmt)
    connector._http = http
    try:
        manifest = connector.build_manifest()
        content = connector.read_file(manifest[0].path, manifest[0].filename)
    finally:
        connector.close()

    assert manifest[0].filename.endswith(extension)
    assert content == b"content"
    assert http.calls[1][0] == f"/api/books/12/export/{export_name}"


def test_read_file_requires_manifest_cache():
    connector = _connector()
    try:
        with pytest.raises(FileNotFoundError):
            connector.read_file("", "missing.md")
    finally:
        connector.close()


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params or {}))
        response = self.routes[path]
        if isinstance(response, list):
            response = response.pop(0)
        if response._request is None:
            response._request = Request("GET", f"https://bookstack.example{path}")
        return response

    def close(self):
        pass


def _connector(**kwargs):
    return BookStackConnector(
        base_url="https://bookstack.example",
        token_id="id",
        token_secret="secret",
        **kwargs,
    )
