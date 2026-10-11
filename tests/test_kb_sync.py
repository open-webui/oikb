import mimetypes
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
import respx
from click.testing import CliRunner

from oikb.client import OikbClient
from oikb.connectors import BaseConnector, ManifestEntry
from oikb.kb_sync import group_entries_by_kb, run_entries_sync
from oikb.sync import SyncCancelled, SyncResult


class Source(BaseConnector):
    def __init__(self, content, fail=False):
        self.content = content
        self.fail = fail
        self.closed = False

    def build_manifest(self):
        if self.fail:
            raise RuntimeError("scan failed")
        return [ManifestEntry(name, "", str(len(data)), len(data)) for name, data in self.content.items()]

    def read_file(self, path, filename):
        assert path == ""  # Destination prefixes must not leak into source reads.
        return self.content[filename]

    def close(self):
        self.closed = True


def test_combined_manifest_filters_routing_and_auth():
    sources = [Source({"readme.txt": b"first", "skip.bin": b"ignored"}), Source({"readme.txt": b"second"})]
    resolver = Mock(side_effect=sources)
    client = Mock()
    client.sync_diff.side_effect = lambda kb, manifest: {"added": manifest}
    entries = [
        {"source": "confluence:ENG", "kb-id": "kb", "filter": {"include": ["*.txt"]}, "auth": {"token": "one"}},
        {"source": "confluence:HR", "kb-id": "kb", "auth": {"token": "two"}},
    ]
    result = run_entries_sync(client, entries, resolve_connector=resolver, quiet=True)
    assert result.added == 2
    assert [e["path"] for e in client.sync_diff.call_args.args[1]] == ["ENG", "HR"]
    assert [call.kwargs["file_content"] for call in client.upload_file.call_args_list] == [b"first", b"second"]
    assert [call.kwargs["auth"] for call in resolver.call_args_list] == [{"token": "one"}, {"token": "two"}]
    client.sync_diff.assert_called_once()
    client.sync_cleanup.assert_not_called()
    assert all(source.closed for source in sources)


@pytest.mark.parametrize("failure", ["scan", "resolve", "duplicate", "filter"])
def test_failure_prevents_any_kb_mutation_and_closes_sources(failure):
    first = Source({"a.txt": b"a"})
    second = Source({"a.txt" if failure == "duplicate" else "b.txt": b"b"}, fail=failure == "scan")
    resolver = Mock(side_effect=[first, RuntimeError("resolve failed") if failure == "resolve" else second])
    entries = [{"source": "one", "kb-id": "kb"}, {"source": "two", "kb-id": "kb"}]
    if failure == "filter":
        entries[1]["filter"] = {"max-size": "bad"}
    client = Mock()
    with pytest.raises((ValueError, RuntimeError)):
        run_entries_sync(client, entries, resolve_connector=resolver, quiet=True)
    assert not client.mock_calls
    assert first.closed
    if failure not in {"resolve", "filter"}:
        assert second.closed


def test_single_source_keeps_paths_and_target_path_is_optional():
    for extra, expected in [({}, ""), ({"target-path": "docs/api"}, "docs/api")]:
        client = Mock()
        client.sync_diff.return_value = {}
        source = Source({"a.txt": b"a"})
        run_entries_sync(client, [{"source": "confluence:ENG", "kb-id": "kb", **extra}], resolve_connector=lambda *a, **kw: source, quiet=True)
        assert client.sync_diff.call_args.args[1][0]["path"] == expected
        assert source.closed


@pytest.mark.parametrize("prefix", ["/absolute", "../parent", "a/../b", "a\\b", "a//b"])
def test_invalid_target_path_fails_before_scan(prefix):
    resolver = Mock()
    with pytest.raises(ValueError, match="target-path"):
        run_entries_sync(Mock(), [{"source": "one", "kb-id": "kb", "target-path": prefix}], resolve_connector=resolver)
    resolver.assert_not_called()


def test_group_validation_and_cancellation():
    with pytest.raises(ValueError, match="source and kb-id"):
        group_entries_by_kb([{"source": "one"}])
    with pytest.raises(ValueError, match="same url and token"):
        group_entries_by_kb([{"source": "one", "kb-id": "kb", "url": "a"}, {"source": "two", "kb-id": "kb", "url": "b"}])
    client, resolver = Mock(), Mock()
    with pytest.raises(SyncCancelled):
        run_entries_sync(client, [{"source": "one", "kb-id": "kb"}], resolve_connector=resolver, cancel_requested=lambda: True)
    assert not client.mock_calls and not resolver.mock_calls


def test_cli_name_selects_entire_kb_group_and_closes_client(monkeypatch):
    import oikb.cli as cli
    import oikb.kb_sync as kb_sync
    entries = [{"name": "one", "source": "one", "kb-id": "kb"}, {"source": "two", "kb-id": "kb"}, {"source": "other", "kb-id": "other"}]
    monkeypatch.setattr(cli, "_load_oikb_yaml", lambda: entries)
    client = Mock()
    monkeypatch.setattr(cli, "_make_client", lambda *a: client)
    sync = Mock(return_value=SyncResult())
    monkeypatch.setattr(kb_sync, "run_entries_sync", sync)
    result = CliRunner().invoke(cli.cli, ["sync", "--name", "one", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert sync.call_args.args[1] == entries[:2]
    assert sync.call_args.kwargs["dry_run"] is True
    client.close.assert_called_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["webhook", "alias", "kb"])
async def test_daemon_triggers_include_sibling_sources(monkeypatch, trigger):
    import oikb.cli as cli
    import oikb.daemon as daemon
    import oikb.kb_sync as kb_sync
    entries = [{"name": "one", "source": "one", "kb-id": "kb"}, {"source": "two", "kb-id": "kb"}]
    monkeypatch.setattr(daemon, "_entries", entries)
    monkeypatch.setattr(daemon, "_sync_locks", {})
    monkeypatch.setattr(daemon, "_scheduler_state", {})
    monkeypatch.setattr(daemon, "_shutdown_event", None)
    monkeypatch.setattr(daemon, "_history", None)
    monkeypatch.setattr(daemon, "_send_notification", AsyncMock())
    monkeypatch.setattr(daemon, "record_sync", Mock())
    client = Mock()
    monkeypatch.setattr(cli, "_make_client", lambda **kw: client)
    sync = Mock(return_value=SyncResult(added=2))
    monkeypatch.setattr(kb_sync, "run_entries_sync", sync)
    if trigger == "webhook":
        await daemon._run_entry(entries[0])  # The same callback registered with webhooks.
        assert all(daemon._scheduler_state[s]["files_added"] == 2 for s in ("one", "two"))
    else:
        result = await daemon.trigger_sync("one" if trigger == "alias" else "kb", dry_run=True)
        assert result["result"]["added"] == 2
        assert daemon._scheduler_state == {}
    assert sync.call_args.args[1] == entries
    client.close.assert_called_once()


def test_upload_error_includes_server_detail():
    client = Mock()
    client.sync_diff.side_effect = lambda kb, manifest: {"added": manifest}
    response = httpx.Response(400, json={"detail": "extraction failed"}, request=httpx.Request("POST", "https://webui.example/files"))
    client.upload_file.side_effect = httpx.HTTPStatusError("Bad Request", request=response.request, response=response)
    result = run_entries_sync(client, [{"source": "one", "kb-id": "kb"}], resolve_connector=lambda *a, **kw: Source({"a.txt": b"a"}), quiet=True)
    assert "extraction failed" in result.errors[0]


@respx.mock
def test_upload_sends_markdown_as_text_without_system_mime_types(monkeypatch):
    monkeypatch.setattr(mimetypes, "guess_type", lambda *a, **kw: (None, None))
    route = respx.post("https://webui.example/api/v1/files/").mock(return_value=httpx.Response(200, json={"id": "f"}))
    OikbClient("https://webui.example", "token").upload_file(b"| a |\n| - |\n", "doc.md", "kb", "hash")
    assert b"Content-Type: text/markdown" in route.calls.last.request.content
