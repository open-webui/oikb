from __future__ import annotations

from typing import Any

import httpx
import respx

from oikb.connectors.sharepoint import SharePointConnector

GRAPH = "https://graph.microsoft.com/v1.0"


def _file(name: str) -> dict[str, Any]:
    return {"name": name, "file": {}, "size": 1}


def _folder(name: str) -> dict[str, Any]:
    return {"name": name, "folder": {}}


def _children(path: str, *pages: list[dict[str, Any]]) -> respx.Route:
    """Serve a children listing split into pages chained by @odata.nextLink, as Graph does."""
    responses = []
    for number, items in enumerate(pages, start=1):
        body: dict[str, Any] = {"value": items}
        if number < len(pages):
            body["@odata.nextLink"] = f"{GRAPH}{path}?$skiptoken={number}"
        responses.append(httpx.Response(200, json=body))
    return respx.get(f"{GRAPH}{path}").mock(side_effect=responses)


@respx.mock
def test_build_manifest_follows_next_link_through_every_page() -> None:
    respx.post("https://login.microsoftonline.com/tenant/oauth2/v2.0/token").respond(
        json={"access_token": "token"}
    )
    respx.get(f"{GRAPH}/sites/contoso.sharepoint.com:/sites/Team").respond(json={"id": "site-id"})
    respx.get(f"{GRAPH}/sites/site-id/drives").respond(
        json={"value": [{"id": "drive-id", "name": "Docs"}]}
    )
    root = _children(
        "/drives/drive-id/root/children",
        [_file("a.pdf"), _folder("Reports")],
        [],  # A page may be empty and still link to the next one.
        [_file("z.pdf"), _folder("Archive")],
    )
    _children("/drives/drive-id/root:/Reports:/children", [_file("q1.xlsx")])
    _children(
        "/drives/drive-id/root:/Archive:/children",
        [_file("2024.xlsx")],
        [_file("2025.xlsx")],
    )

    connector = SharePointConnector(
        site="contoso.sharepoint.com",
        site_path="sites/Team",
        library="Docs",
        tenant_id="tenant",
        client_id="client",
        client_secret="secret",
    )
    try:
        manifest = connector.build_manifest()
    finally:
        connector.close()

    assert [entry.display_path for entry in manifest] == [
        "Archive/2024.xlsx",
        "Archive/2025.xlsx",
        "Reports/q1.xlsx",
        "a.pdf",
        "z.pdf",
    ]
    assert [str(call.request.url) for call in root.calls] == [
        f"{GRAPH}/drives/drive-id/root/children",
        f"{GRAPH}/drives/drive-id/root/children?$skiptoken=1",
        f"{GRAPH}/drives/drive-id/root/children?$skiptoken=2",
    ]
