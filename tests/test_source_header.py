from unittest.mock import Mock

import httpx
import pytest
import respx
from click.testing import CliRunner

from oikb.connectors import BaseConnector, ManifestEntry
from oikb.connectors.confluence import ConfluenceConnector
from oikb.connectors.notion import NotionConnector
from oikb.kb_sync import run_entries_sync
from oikb.source_header import SourceHeaderConnector, build_source_header

URL = "https://example.atlassian.net/wiki/pages/viewpage.action?pageId=1"


class Source(BaseConnector):
    def __init__(self, entries, content=b"body"):
        self.entries = entries
        self.content = content
        self.closed = False

    def build_manifest(self):
        return list(self.entries)

    def read_file(self, path, filename):
        return self.content

    def close(self):
        self.closed = True


def _entry(filename="page.txt", source_url=URL, last_modified="2026-11-20T09:15:00Z", checksum="abc"):
    return ManifestEntry(filename, "", checksum, 0, source_url=source_url, last_modified=last_modified)


def _sync(entries, source, **entry_options):
    client = Mock()
    client.sync_diff.side_effect = lambda kb, manifest: {"added": manifest}
    run_entries_sync(
        client,
        [{"source": "one", "kb-id": "kb", **entry_options}],
        resolve_connector=lambda *a, **kw: source,
        quiet=True,
    )
    return client


def test_header_lists_url_and_last_edited():
    assert build_source_header(_entry()) == f"Source: {URL}\nLast edited: 2026-11-20T09:15:00Z\n\n"


@pytest.mark.parametrize(
    "entry",
    [
        _entry(filename="report.pdf"),
        _entry(source_url=None, last_modified=None),
        _entry(source_url="javascript:alert(1)", last_modified=None),
        _entry(source_url="https://example.com/a\nInjected: line", last_modified=None),
        _entry(source_url="https://example.com/a b", last_modified=None),
        _entry(source_url=None, last_modified="2026-11-20\nInjected"),
    ],
)
def test_no_header_for_binary_files_or_unsafe_values(entry):
    assert build_source_header(entry) is None


def test_header_skips_invalid_url_but_keeps_valid_timestamp():
    assert build_source_header(_entry(source_url="ftp://example.com/a")) == "Last edited: 2026-11-20T09:15:00Z\n\n"


def test_option_prepends_header_and_changes_checksum():
    entry = _entry()
    source = Source([entry])
    client = _sync([entry], source, **{"include-source-header": True})
    uploaded = client.upload_file.call_args.kwargs
    assert uploaded["file_content"] == f"Source: {URL}\nLast edited: 2026-11-20T09:15:00Z\n\nbody".encode()
    assert uploaded["file_hash"] != "abc"
    assert source.closed


def test_option_off_keeps_content_and_checksum():
    entry = _entry()
    client = _sync([entry], Source([entry]))
    uploaded = client.upload_file.call_args.kwargs
    assert uploaded["file_content"] == b"body"
    assert uploaded["file_hash"] == "abc"
    assert "source_url" not in client.sync_diff.call_args.args[1][0]


def test_url_change_alone_changes_checksum():
    first = SourceHeaderConnector(Source([_entry()])).build_manifest()[0]
    moved = SourceHeaderConnector(Source([_entry(source_url=URL + "0")])).build_manifest()[0]
    assert first.checksum != moved.checksum


def test_empty_content_stays_empty():
    connector = SourceHeaderConnector(Source([_entry()], content=b""))
    connector.build_manifest()
    assert connector.read_file("", "page.txt") == b""


def test_invalid_option_fails_before_scan():
    resolver = Mock()
    with pytest.raises(ValueError, match="include-source-header"):
        run_entries_sync(
            Mock(),
            [{"source": "one", "kb-id": "kb", "include-source-header": "yes"}],
            resolve_connector=resolver,
        )
    resolver.assert_not_called()


def test_validate_rejects_non_boolean_option(tmp_path):
    import yaml

    import oikb.cli as cli

    (tmp_path / "docs").mkdir()
    config = tmp_path / "oikb.yaml"
    config.write_text(yaml.safe_dump({"sources": [
        {"name": "one", "source": str(tmp_path / "docs"), "kb-id": "kb", "include-source-header": "yes"},
    ]}))
    result = CliRunner().invoke(cli.cli, ["validate", "--config", str(config)])
    assert result.exit_code == 1
    assert "include-source-header must be true or false" in result.output


def test_cli_flag_wraps_single_source(monkeypatch):
    import oikb.cli as cli
    import oikb.sync as sync_module

    source = Source([_entry()])
    monkeypatch.setattr(cli, "_resolve_connector", lambda *a, **kw: source)
    monkeypatch.setattr(cli, "_make_client", lambda *a: Mock())
    run_sync = Mock(return_value=sync_module.SyncResult())
    monkeypatch.setattr(sync_module, "run_sync", run_sync)
    result = CliRunner().invoke(cli.cli, ["sync", "one", "--kb-id", "kb", "--include-source-header"])
    assert result.exit_code == 0, result.output
    assert isinstance(run_sync.call_args.kwargs["connector"], SourceHeaderConnector)


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


@respx.mock
def test_notion_reports_page_url_and_last_edited_time():
    respx.post("https://api.notion.com/v1/databases/root/query").mock(return_value=httpx.Response(200, json={
        "results": [{
            "id": "page-1",
            "url": "https://www.notion.so/Guide-page1",
            "last_edited_time": "2026-11-20T09:15:00.000Z",
            "properties": {"Name": {"type": "title", "title": [{"plain_text": "Guide"}]}},
        }],
        "has_more": False,
    }))
    connector = NotionConnector("root", token="token")
    try:
        [entry] = connector.build_manifest()
    finally:
        connector.close()
    assert entry.source_url == "https://www.notion.so/Guide-page1"
    assert entry.last_modified == "2026-11-20T09:15:00.000Z"
