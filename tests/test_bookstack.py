import pytest
import respx
from httpx import Response

from oikb.connectors.bookstack import BookStackConnector, parse_bookstack_source


def test_parse_bookstack_source_without_book_id():
    assert parse_bookstack_source("bookstack:") == {"book_id": None}


def test_parse_bookstack_source_with_book_id():
    assert parse_bookstack_source("bookstack:12") == {"book_id": "12"}


@pytest.mark.parametrize("source", ["bookstack:abc", "bookstack:12,34", "bookstack:12/34"])
def test_parse_bookstack_source_rejects_invalid_book_id(source):
    with pytest.raises(ValueError, match="numeric book ID"):
        parse_bookstack_source(source)


@respx.mock
def test_build_manifest_filters_by_book_id():
    route = respx.get("https://bookstack.example/api/pages").mock(
        return_value=Response(
            200,
            json={
                "data": [
                    {
                        "id": 99,
                        "book_id": 12,
                        "name": "Filtered Page",
                        "updated_at": "2026-07-14T12:00:00Z",
                    }
                ],
                "total": 1,
            },
        )
    )

    connector = BookStackConnector(
        base_url="https://bookstack.example",
        token_id="id",
        token_secret="secret",
        book_id="12",
    )
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    params = route.calls[0].request.url.params
    assert params["count"] == "100"
    assert params["offset"] == "0"
    assert params["filter[book_id]"] == "12"
    assert len(manifest) == 1
    assert manifest[0].filename == "99_Filtered Page.txt"


@respx.mock
def test_build_manifest_without_book_id_keeps_unfiltered_request():
    route = respx.get("https://bookstack.example/api/pages").mock(
        return_value=Response(200, json={"data": [], "total": 0})
    )

    connector = BookStackConnector(
        base_url="https://bookstack.example",
        token_id="id",
        token_secret="secret",
    )
    try:
        assert connector.build_manifest() == []
    finally:
        connector.close()

    params = route.calls[0].request.url.params
    assert params["count"] == "100"
    assert params["offset"] == "0"
    assert "filter[book_id]" not in params
