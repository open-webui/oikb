"""Gitea connector — sync Gitea repos to a Knowledge Base via the API.

Uses the Gitea Git Trees API for checksums (blob SHAs) — no local clone needed.
Set GITEA_URL to your instance URL, and GITEA_TOKEN for private repositories.
"""

from __future__ import annotations

import os

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


class GiteaConnector(BaseConnector):
    """Sync files from one Gitea repository, or all repos for an owner.

    Args:
        owner:    Repository owner or organization.
        repo:     Repository name, or "*" for all repos owned by owner.
        branch:   Branch to sync from (default: repo default branch).
        path:     Subdirectory to scope to (e.g. "docs/").
        token:    Gitea personal access token (or GITEA_TOKEN env var).
        base_url: Gitea instance URL (or GITEA_URL env var).
    """

    def __init__(
        self,
        owner: str,
        repo: str,
        branch: str | None = None,
        path: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.path = path.strip("/") if path else None
        self._token = token or os.environ.get("GITEA_TOKEN")
        self._base_url = (base_url or os.environ.get("GITEA_URL") or "").rstrip("/")
        self._default_branches: dict[str, str] = {}

        if not self._base_url:
            raise ValueError("GITEA_URL is required for gitea: sources (e.g. https://gitea.example.com)")

        headers: dict[str, str] = {"Accept": "application/json"}
        if self._token:
            headers["Authorization"] = f"token {self._token}"

        self._http = httpx.Client(
            base_url=f"{self._base_url}/api/v1",
            headers=headers,
            timeout=60.0,
        )

    def build_manifest(self) -> list[ManifestEntry]:
        """Fetch the repo tree and build a manifest.

        Gitea paginates the recursive tree endpoint. Blob SHAs are used as
        checksums because they are content-addressable hashes.
        """
        if self._all_repos:
            entries: list[ManifestEntry] = []
            for repo in self._list_repos():
                entries.extend(self._build_repo_manifest(repo, prefix_repo=True))
            entries.sort(key=lambda e: e.display_path)
            return entries

        return self._build_repo_manifest(self.repo)

    def _build_repo_manifest(self, repo: str, prefix_repo: bool = False) -> list[ManifestEntry]:
        """Fetch one repo tree and build manifest entries."""
        ref = self.branch or self._get_default_branch(repo)
        entries: list[ManifestEntry] = []
        seen_items = 0
        page = 1

        while True:
            resp = self._http.get(
                f"/repos/{self.owner}/{repo}/git/trees/{ref}",
                params={"recursive": "true", "per_page": 100, "page": page},
            )
            resp.raise_for_status()
            tree = resp.json()
            items = tree.get("tree", [])

            if not items:
                break

            seen_items += len(items)

            for item in items:
                if item.get("type") != "blob":
                    continue

                file_path = item["path"]

                # Filter by path prefix if specified.
                if self.path:
                    if not file_path.startswith(self.path + "/"):
                        continue
                    # Strip the prefix so paths are relative to the scoped dir.
                    file_path = file_path[len(self.path) + 1 :]

                parts = file_path.rsplit("/", 1)
                if len(parts) == 2:
                    dir_path, filename = parts
                else:
                    dir_path, filename = "", parts[0]

                if prefix_repo:
                    dir_path = f"{repo}/{dir_path}" if dir_path else repo

                entries.append(
                    ManifestEntry(
                        filename=filename,
                        path=dir_path,
                        checksum=item["sha"],  # Git blob SHA — content-addressable.
                        size=item.get("size", 0),
                    )
                )

            total_count = tree.get("total_count")
            if total_count is not None:
                if seen_items >= total_count:
                    break
            elif len(items) < 100:
                break

            page += 1

        entries.sort(key=lambda e: e.display_path)
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        """Download a file's raw content via the Gitea raw file endpoint."""
        file_path = f"{path}/{filename}" if path else filename
        repo = self.repo

        if self._all_repos:
            repo, _, file_path = file_path.partition("/")
            if not repo or not file_path:
                raise ValueError(f"Invalid wildcard Gitea path: {path}/{filename}")

        if self.path:
            file_path = f"{self.path}/{file_path}"

        ref = self.branch or self._get_default_branch(repo)

        resp = self._http.get(
            f"/repos/{self.owner}/{repo}/raw/{file_path}",
            params={"ref": ref},
        )
        resp.raise_for_status()
        return resp.content

    @property
    def _all_repos(self) -> bool:
        return self.repo == "*"

    def _get_default_branch(self, repo: str) -> str:
        """Fetch and cache the repo's default branch name."""
        if repo not in self._default_branches:
            resp = self._http.get(f"/repos/{self.owner}/{repo}")
            resp.raise_for_status()
            self._default_branches[repo] = resp.json()["default_branch"]
        return self._default_branches[repo]

    def _list_repos(self) -> list[str]:
        """List repositories for the configured owner or organization."""
        repos: list[str] = []
        page = 1

        while True:
            resp = self._http.get(f"/orgs/{self.owner}/repos", params={"page": page, "limit": 50})
            if resp.status_code == 404:
                resp = self._http.get(f"/users/{self.owner}/repos", params={"page": page, "limit": 50})
            resp.raise_for_status()
            items = resp.json()

            if not items:
                break

            repos.extend(repo["name"] for repo in items)

            if len(items) < 50:
                break
            page += 1

        repos.sort()
        return repos

    def close(self) -> None:
        self._http.close()


def parse_gitea_source(source: str) -> dict[str, str | None]:
    """Parse a gitea:owner/repo[/path] source string.

    Examples:
        gitea:myorg/docs
        gitea:myorg/docs/api
        gitea:myorg/*
    """
    source = source.removeprefix("gitea:")

    parts = source.split("/", 2)
    if len(parts) < 2:
        raise ValueError(f"Invalid Gitea source: {source}. Expected: gitea:owner/repo")

    owner = parts[0]
    repo = parts[1]
    path = parts[2] if len(parts) > 2 else None

    return {"owner": owner, "repo": repo, "path": path}