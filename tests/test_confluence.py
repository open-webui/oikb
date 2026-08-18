from __future__ import annotations

import httpx
import pytest
import respx

from oikb.connectors.confluence import ConfluenceConnector, parse_confluence_source
from oikb.sync import build_manifest_filter


def test_parse_hierarchical_source() -> None:
    assert parse_confluence_source("confluence:ABC?structure=hierarchical") == {
        "base_url": None,
        "space_key": "ABC",
        "structure": "hierarchical",
    }


def test_parse_url_source_with_structure() -> None:
    assert parse_confluence_source(
        "https://company.atlassian.net/ABC?structure=hierarchical"
    ) == {
        "base_url": "https://company.atlassian.net",
        "space_key": "ABC",
        "structure": "hierarchical",
    }


@pytest.mark.parametrize(
    "source",
    [
        "confluence:ABC?structure=invalid",
        "confluence:ABC?unexpected=value",
    ],
)
def test_parse_rejects_invalid_structure(source: str) -> None:
    with pytest.raises(ValueError):
        parse_confluence_source(source)


@respx.mock
def test_resolves_space_key_to_id() -> None:
    lookup = respx.get("https://test.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": "123456789", "key": "ABC"}]}
        )
    )
    pages = respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": []}))

    connector = ConfluenceConnector(
        space_key="ABC", base_url="https://test.atlassian.net", token="token"
    )
    try:
        assert connector.build_manifest() == []
    finally:
        connector.close()

    assert lookup.calls[0].request.url.params["keys"] == "ABC"
    assert pages.called


@respx.mock
def test_rejects_mismatched_space_lookup() -> None:
    respx.get("https://test.atlassian.net/wiki/api/v2/spaces").mock(
        return_value=httpx.Response(
            200, json={"results": [{"id": "123456789", "key": "OTHER"}]}
        )
    )

    with pytest.raises(ValueError, match="Confluence space 'ABC' not found"):
        ConfluenceConnector(
            space_key="ABC",
            base_url="https://test.atlassian.net",
            token="token",
        )


def _pages() -> list[dict[str, object]]:
    return [
        {"id": "1", "title": "FAQ", "version": {"number": 1}},
        {
            "id": "2",
            "title": "Benefits",
            "parentId": "1",
            "version": {"number": 2},
        },
        {"id": "3", "title": "Internal Notes", "version": {"number": 1}},
    ]


@pytest.mark.parametrize(
    ("structure", "expected"),
    [
        ("flat", ["Benefits.txt", "FAQ.txt", "Internal Notes.txt"]),
        ("hierarchical", ["FAQ.txt", "FAQ/Benefits.txt", "Internal Notes.txt"]),
    ],
)
@respx.mock
def test_manifest_in_both_modes(structure: str, expected: list[str]) -> None:
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": _pages()}))

    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure=structure,
    )
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert [entry.display_path for entry in manifest] == expected


@respx.mock
def test_hierarchical_paths_work_with_filter_and_read_file() -> None:
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": _pages()}))
    content = respx.get("https://test.atlassian.net/wiki/api/v2/pages/2").mock(
        return_value=httpx.Response(
            200, json={"body": {"storage": {"value": "<p>Benefits</p>"}}}
        )
    )

    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure="hierarchical",
    )
    try:
        manifest = connector.build_manifest()
        selected = build_manifest_filter(include=["FAQ*"])(manifest)
        assert [entry.display_path for entry in selected] == [
            "FAQ.txt",
            "FAQ/Benefits.txt",
        ]
        assert connector.read_file("FAQ", "Benefits.txt") == b"Benefits"
    finally:
        connector.close()

    assert content.called


@respx.mock
def test_hierarchical_mode_rejects_duplicate_paths() -> None:
    pages = [
        {"id": "1", "title": "FAQ", "version": {"number": 1}},
        {"id": "2", "title": "FAQ", "version": {"number": 1}},
    ]
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": pages}))
    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure="hierarchical",
    )

    try:
        with pytest.raises(ValueError, match="Duplicate Confluence page path"):
            connector.build_manifest()
    finally:
        connector.close()


@respx.mock
def test_manifest_can_be_built_repeatedly() -> None:
    respx.get(
        "https://test.atlassian.net/wiki/api/v2/spaces/123456789/pages"
    ).mock(return_value=httpx.Response(200, json={"results": _pages()}))
    connector = ConfluenceConnector(
        space_key="123456789",
        base_url="https://test.atlassian.net",
        token="token",
        structure="hierarchical",
    )

    try:
        first = connector.build_manifest()
        second = connector.build_manifest()
    finally:
        connector.close()

    assert first == second
