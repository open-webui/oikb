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
def test_hierarchical_mode_disambiguates_duplicate_paths() -> None:
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
        assert [e.display_path for e in connector.build_manifest()] == ["FAQ_1.txt", "FAQ_2.txt"]
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


@pytest.mark.parametrize("structure", ["flat", "hierarchical"])
@respx.mock
def test_v1_pagination_context_path_auth_and_content(structure, monkeypatch):
    monkeypatch.setenv("CONFLUENCE_API_VERSION", "v1")
    monkeypatch.setenv("CONFLUENCE_USER", "")
    endpoint = "https://wiki.example/confluence/rest/api/content"
    route = respx.get(endpoint).mock(side_effect=[
        httpx.Response(200, json={
            "results": [{"id": "1", "title": "Parent", "version": {"number": 1}}],
            # The server caps the requested 250 to 1. The next link is authoritative.
            "limit": 1, "_links": {"next": "/rest/api/content?start=1&limit=1"},
        }),
        httpx.Response(200, json={"results": [{
            "id": "2", "title": "Child", "version": {"number": 3},
            "ancestors": [{"id": "1", "title": "Parent"}],
        }]}),
    ])
    content = respx.get(endpoint + "/2").respond(200, json={
        "body": {"storage": {"value": '<ac:plain-text-body><![CDATA[if x < 2:\n    run()]]></ac:plain-text-body>'}},
    })
    with ConfluenceConnector("ENG", base_url="https://wiki.example/confluence", token="pat", structure=structure) as connector:
        manifest = connector.build_manifest()
        child = next(e for e in manifest if e.filename == "Child.txt")
        assert child.path == ("Parent" if structure == "hierarchical" else "")
        assert connector.read_file(child.path, child.filename) == b"if x < 2:\n    run()"
    assert [call.request.url.params["start"] for call in route.calls] == ["0", "1"]
    assert route.calls[0].request.url.params["spaceKey"] == "ENG"
    assert route.calls[0].request.url.params["expand"] == "ancestors,version"
    assert content.calls[0].request.headers["Authorization"] == "Bearer pat"
    assert content.calls[0].request.url.params["expand"] == "body.storage"


@respx.mock
def test_v2_cursor_is_decoded_and_wiki_path_is_not_duplicated():
    route = respx.get("https://test.atlassian.net/wiki/api/v2/spaces/123/pages").mock(side_effect=[
        httpx.Response(200, json={"results": [{"id": "1", "title": "One"}], "_links": {"next": "?cursor=a%2Bb%3D"}}),
        httpx.Response(200, json={"results": [{"id": "2", "title": "Two"}]}),
    ])
    with ConfluenceConnector("123", base_url="https://test.atlassian.net/wiki", user="email", token="secret") as connector:
        assert len(connector.build_manifest()) == 2
    assert route.calls[1].request.url.params["cursor"] == "a+b="
    assert route.calls[0].request.headers["Authorization"] == "Basic ZW1haWw6c2VjcmV0"


@respx.mock
def test_colliding_names_are_stable_and_route_to_distinct_pages():
    pages = [
        {"id": "1", "title": "A/B"}, {"id": "2", "title": "A:B"},
        {"id": "3", "title": "A_B_1"},
    ]
    respx.get("https://wiki.example/wiki/api/v2/spaces/123/pages").mock(side_effect=[
        httpx.Response(200, json={"results": pages}),
        httpx.Response(200, json={"results": list(reversed(pages))}),
    ])
    for page in pages:
        respx.get(f"https://wiki.example/wiki/api/v2/pages/{page['id']}").respond(
            200, json={"body": {"storage": {"value": page["title"]}}},
        )
    with ConfluenceConnector("123", base_url="https://wiki.example", token="pat") as connector:
        manifest = connector.build_manifest()
        assert len({e.display_path for e in manifest}) == 3
        assert {connector.read_file(e.path, e.filename) for e in manifest} == {b"A/B", b"A:B", b"A_B_1"}
        assert connector.build_manifest() == manifest


@pytest.mark.parametrize("next_link", ["?limit=1", "?start=0", "?start=-1"])
@respx.mock
def test_invalid_pagination_fails_instead_of_returning_partial_manifest(next_link):
    respx.get("https://wiki.example/rest/api/content").respond(200, json={
        "results": [{"id": "1", "title": "One"}], "_links": {"next": next_link},
    })
    with ConfluenceConnector("ENG", base_url="https://wiki.example", token="pat", api_version="v1") as connector:
        with pytest.raises(ValueError, match="pagination"):
            connector.build_manifest()


@respx.mock
def test_empty_pages_remain_skipped():
    from oikb.connectors import SourceFileUnavailable
    respx.get("https://wiki.example/rest/api/content").respond(200, json={"results": [{"id": "1", "title": "Index"}]})
    respx.get("https://wiki.example/rest/api/content/1").respond(200, json={
        "body": {"storage": {"value": '<ac:structured-macro ac:name="children"/>'}},
    })
    with ConfluenceConnector("ENG", base_url="https://wiki.example", token="pat", api_version="v1") as connector:
        connector.build_manifest()
        with pytest.raises(SourceFileUnavailable):
            connector.read_file("", "Index.txt")


def test_source_context_path_and_invalid_version():
    assert parse_confluence_source("confluence:https://wiki.example/confluence/ENG")["base_url"] == "https://wiki.example/confluence"
    with pytest.raises(ValueError, match="API_VERSION"):
        ConfluenceConnector("ENG", api_version="v3")


@respx.mock
def test_cli_passes_source_auth_and_can_override_basic_env(monkeypatch):
    from oikb.cli import _resolve_connector
    monkeypatch.setenv("CONFLUENCE_USER", "cloud-user")
    route = respx.get("https://wiki.example/rest/api/content").respond(200, json={"results": []})
    with _resolve_connector("confluence:ENG", auth={
        "base_url": "https://wiki.example", "user": "", "token": "pat", "api_version": "v1",
    }) as connector:
        connector.build_manifest()
    assert route.calls[0].request.headers["Authorization"] == "Bearer pat"


@pytest.mark.parametrize(
    ("api_version", "base_url", "endpoint", "version", "expected_url", "expected_date"),
    [
        (
            "v2", "https://test.atlassian.net", "https://test.atlassian.net/wiki/api/v2/spaces/123/pages",
            {"number": 3, "createdAt": "2026-11-20T09:15:00.000Z"},
            "https://test.atlassian.net/wiki/pages/viewpage.action?pageId=7", "2026-11-20T09:15:00.000Z",
        ),
        (
            "v1", "https://wiki.example.com/confluence", "https://wiki.example.com/confluence/rest/api/content",
            {"number": 3, "when": "2026-11-20T09:15:00.000+01:00"},
            "https://wiki.example.com/confluence/pages/viewpage.action?pageId=7", "2026-11-20T09:15:00.000+01:00",
        ),
    ],
)
@respx.mock
def test_confluence_reports_page_url_and_version_date(api_version, base_url, endpoint, version, expected_url, expected_date):
    respx.get(endpoint).mock(return_value=httpx.Response(200, json={
        "results": [{"id": "7", "title": "VPN", "version": version}],
    }))
    connector = ConfluenceConnector(space_key="123", base_url=base_url, token="token", api_version=api_version)
    try:
        [entry] = connector.build_manifest()
    finally:
        connector.close()
    assert entry.source_url == expected_url
    assert entry.last_modified == expected_date
