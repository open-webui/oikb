from __future__ import annotations

import pytest
import respx
from httpx import Response

from oikb.cli import _resolve_connector
from oikb.connectors.gitea import GiteaConnector, parse_gitea_source


def test_parse_gitea_source_with_path() -> None:
    assert parse_gitea_source("gitea:owner/repo/docs/api") == {
        "owner": "owner",
        "repo": "repo",
        "path": "docs/api",
    }


def test_parse_gitea_source_with_wildcard() -> None:
    assert parse_gitea_source("gitea:owner/*") == {
        "owner": "owner",
        "repo": "*",
        "path": None,
    }


def test_parse_gitea_source_rejects_missing_repo() -> None:
    with pytest.raises(ValueError, match="Expected: gitea:owner/repo"):
        parse_gitea_source("gitea:owner")


def test_gitea_connector_requires_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITEA_URL", raising=False)

    with pytest.raises(ValueError, match="GITEA_URL is required"):
        GiteaConnector(owner="owner", repo="repo")


def test_resolve_connector_dispatches_gitea(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITEA_URL", "https://gitea.example.com")

    connector = _resolve_connector("gitea:owner/repo/docs", branch="main")

    assert isinstance(connector, GiteaConnector)
    assert connector.owner == "owner"
    assert connector.repo == "repo"
    assert connector.branch == "main"
    assert connector.path == "docs"
    connector.close()


@respx.mock
def test_build_manifest_scopes_path_and_paginates() -> None:
    base = "https://gitea.example.com/api/v1"
    respx.get(f"{base}/repos/owner/repo/git/trees/main").mock(
        side_effect=[
            Response(
                200,
                json={
                    "total_count": 3,
                    "tree": [
                        {"path": "README.md", "type": "blob", "sha": "root", "size": 10},
                        {"path": "docs/a.md", "type": "blob", "sha": "sha-a", "size": 20},
                    ],
                },
            ),
            Response(
                200,
                json={
                    "total_count": 3,
                    "tree": [
                        {"path": "docs/sub/b.md", "type": "blob", "sha": "sha-b", "size": 30},
                    ],
                },
            ),
        ]
    )

    connector = GiteaConnector(
        owner="owner",
        repo="repo",
        branch="main",
        path="docs",
        base_url="https://gitea.example.com",
    )

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["a.md", "sub/b.md"]
    assert [entry.checksum for entry in manifest] == ["sha-a", "sha-b"]
    assert [entry.size for entry in manifest] == [20, 30]


@respx.mock
def test_build_manifest_with_wildcard_prefixes_repo_names() -> None:
    base = "https://gitea.example.com/api/v1"
    respx.get(f"{base}/orgs/owner/repos").mock(
        return_value=Response(200, json=[{"name": "repo-a"}, {"name": "repo-b"}])
    )
    respx.get(f"{base}/repos/owner/repo-a").mock(return_value=Response(200, json={"default_branch": "main"}))
    respx.get(f"{base}/repos/owner/repo-b").mock(return_value=Response(200, json={"default_branch": "trunk"}))
    respx.get(f"{base}/repos/owner/repo-a/git/trees/main").mock(
        return_value=Response(
            200,
            json={"total_count": 1, "tree": [{"path": "README.md", "type": "blob", "sha": "a", "size": 1}]},
        )
    )
    respx.get(f"{base}/repos/owner/repo-b/git/trees/trunk").mock(
        return_value=Response(
            200,
            json={"total_count": 1, "tree": [{"path": "docs/index.md", "type": "blob", "sha": "b", "size": 2}]},
        )
    )

    connector = GiteaConnector(owner="owner", repo="*", base_url="https://gitea.example.com")

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == ["repo-a/README.md", "repo-b/docs/index.md"]
    assert [entry.checksum for entry in manifest] == ["a", "b"]


@respx.mock
def test_read_file_downloads_raw_content_with_path_scope() -> None:
    base = "https://gitea.example.com/api/v1"
    route = respx.get(f"{base}/repos/owner/repo/raw/docs/sub/file.md").mock(
        return_value=Response(200, content=b"hello")
    )

    connector = GiteaConnector(
        owner="owner",
        repo="repo",
        branch="main",
        path="docs",
        base_url="https://gitea.example.com",
    )

    assert connector.read_file("sub", "file.md") == b"hello"
    assert route.calls.last.request.url.params["ref"] == "main"


@respx.mock
def test_read_file_with_wildcard_routes_to_repo_from_path() -> None:
    base = "https://gitea.example.com/api/v1"
    respx.get(f"{base}/repos/owner/repo-a").mock(return_value=Response(200, json={"default_branch": "main"}))
    route = respx.get(f"{base}/repos/owner/repo-a/raw/docs/file.md").mock(
        return_value=Response(200, content=b"hello")
    )

    connector = GiteaConnector(owner="owner", repo="*", base_url="https://gitea.example.com")

    assert connector.read_file("repo-a/docs", "file.md") == b"hello"
    assert route.calls.last.request.url.params["ref"] == "main"