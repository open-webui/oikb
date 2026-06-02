from __future__ import annotations

from click.testing import CliRunner

from oikb import cli as cli_module
from oikb.cli import cli


class DummyClient:
    def get_kb(self, kb_id: str) -> dict:
        return {
            "id": kb_id,
            "name": "Docs",
            "description": "",
            "files": None,
        }

    def close(self) -> None:
        pass


def test_status_handles_null_files(monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "resolve_url", lambda value=None: "https://openwebui.example.com")
    monkeypatch.setattr(cli_module, "resolve_token", lambda value=None: "token")
    monkeypatch.setattr("oikb.client.OikbClient", lambda *args, **kwargs: DummyClient())

    result = CliRunner().invoke(cli, ["status", "--kb-id", "kb123"])

    assert result.exit_code == 0
    assert "Knowledge Base: Docs" in result.output
    assert "Files:       0" in result.output
