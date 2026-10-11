from __future__ import annotations

from urllib.parse import quote
from xml.etree import ElementTree

import pytest
import respx

from oikb.connectors.nextcloud import NextcloudConnector


def _listing(items: list[tuple[str, bytes | None]]) -> bytes:
    """Build a DAV listing; None denotes a folder."""
    root = ElementTree.Element("{DAV:}multistatus")
    for href, data in items:
        response = ElementTree.SubElement(root, "{DAV:}response")
        ElementTree.SubElement(response, "{DAV:}href").text = href
        propstat = ElementTree.SubElement(response, "{DAV:}propstat")
        ElementTree.SubElement(propstat, "{DAV:}status").text = "HTTP/1.1 200 OK"
        prop = ElementTree.SubElement(propstat, "{DAV:}prop")
        resource_type = ElementTree.SubElement(prop, "{DAV:}resourcetype")
        if data is None:
            ElementTree.SubElement(resource_type, "{DAV:}collection")
        else:
            ElementTree.SubElement(prop, "{DAV:}getetag").text = '"test-etag"'
            ElementTree.SubElement(prop, "{DAV:}getcontentlength").text = str(len(data))
    return ElementTree.tostring(root)


@pytest.mark.parametrize(
    ("user_id", "href_user", "accepted"),
    [
        ("user@example.com", "user@example.com", True),
        ("user@example.com", "user%40example.com", True),
        ("user name", "user name", True),
        ("user\u00e4", "user%c3%a4", True),
        ("user%40", "user%2540", True),
        ("user@example.com", "user@example.com-other", False),
        ("user@example.com", "other@example.com", False),
    ],
)
@respx.mock(assert_all_called=False)
def test_manifest_and_download_handle_path_encoding(
    user_id: str, href_user: str, accepted: bool, respx_mock: respx.MockRouter
) -> None:
    base_url = "https://example.test/nextcloud"
    dav_url = f"{base_url}/remote.php/dav/files/{quote(user_id, safe='')}/Documents"
    href_root = f"{base_url}/remote.php/dav/files/{href_user}/Documents"
    files = {"report final.txt": b"space", "report%20final.txt": b"percent"}

    lookup = respx_mock.get(f"{base_url}/ocs/v2.php/cloud/user?format=json").respond(
        200, json={"ocs": {"data": {"id": user_id}}}
    )
    root_listing = _listing([
        (f"{href_root}/", None),
        (f"{href_root}/Nested%20folder/", None),
    ])
    nested_listing = _listing([
        (f"{href_root}/Nested%20folder/{quote(name, safe='')}", content)
        for name, content in files.items()
    ])
    respx_mock.request("PROPFIND", dav_url).respond(207, content=root_listing)
    nested = respx_mock.request("PROPFIND", f"{dav_url}/Nested%20folder").respond(
        207, content=nested_listing
    )
    for name, content in files.items():
        respx_mock.get(f"{dav_url}/Nested%20folder/{quote(name, safe='')}").respond(
            200, content=content
        )

    connector = NextcloudConnector(
        "/Documents", base_url=base_url, user="login-alias", password="test"
    )
    try:
        manifest = connector.build_manifest()
        expected = [f"Nested folder/{name}" for name in files] if accepted else []
        assert [entry.display_path for entry in manifest] == expected
        assert nested.called == accepted
        for entry in manifest:
            assert connector.read_file(entry.path, entry.filename) == files[entry.filename]
    finally:
        connector.close()

    assert lookup.call_count == 1
    assert lookup.calls[0].request.headers["OCS-APIRequest"] == "true"
