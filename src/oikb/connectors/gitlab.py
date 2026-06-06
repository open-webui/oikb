"""GitLab connector — sync a GitLab repo to a Knowledge Base via the API.

Requires: pip install oikb[gitlab]  (uses httpx, already a core dep)
Uses the GitLab Repository Tree API — no local clone needed.
"""

from __future__ import annotations

import os
import urllib.parse

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


class GitLabConnector(BaseConnector):
    """Sync files from a GitLab repository.

    Args:
        owner:    Project namespace (e.g. "open-webui") OR full unresolved path.
        repo:     Project name (e.g. "docs"). If None, owner is treated as a full path to resolve.
        branch:   Branch to sync from (default: project default branch).
        path:     Subdirectory to scope to (e.g. "docs/").
        token:    GitLab personal access token (or GITLAB_TOKEN env var).
        base_url: GitLab instance URL (default: https://gitlab.com).
    """

    def __init__(
        self,
        owner: str,
        repo: str | None = None,
        branch: str | None = None,
        path: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.path = path.strip("/") if path else None
        self._token = token or os.environ.get("GITLAB_TOKEN")
        self._base_url = (
            base_url or os.environ.get("GITLAB_URL", "https://gitlab.com")
        ).rstrip("/")

        headers: dict[str, str] = {}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token

        self._http = httpx.Client(
            base_url=f"{self._base_url}/api/v4",
            headers=headers,
            timeout=60.0,
        )

        self._project_id: str | None = None

        # If repo is explicitly provided, we don't need dynamic resolution.
        if self.repo:
            self._project_id = f"{urllib.parse.quote(self.owner, safe='')}%2F{urllib.parse.quote(self.repo, safe='')}"

    def _ensure_resolved(self) -> None:
        """Lazily resolve the project path if it wasn't provided explicitly."""
        if self._project_id is not None:
            return

        full_path = self.owner.strip("/")
        segments = full_path.split("/")

        if len(segments) < 2:
            self._project_id = urllib.parse.quote(full_path, safe="")
            return

        # Try segment prefixes to find the actual project ID via API
        for i in range(2, len(segments) + 1):
            project_candidate = "/".join(segments[:i])
            encoded_candidate = urllib.parse.quote(project_candidate, safe="")

            resp = self._http.get(f"/projects/{encoded_candidate}")

            if resp.status_code == 200:
                self.owner = "/".join(segments[: i - 1])
                self.repo = segments[i - 1]
                self._project_id = encoded_candidate

                # If there are remaining segments, they belong to the file path
                remaining = segments[i:]
                if remaining:
                    resolved_path = "/".join(remaining)
                    # Merge with explicitly passed path if both exist
                    self.path = (
                        f"{resolved_path}/{self.path}" if self.path else resolved_path
                    )
                return
            elif resp.status_code != 404:
                # If it's a 401, 403, or 500, we should fail loudly, not silently skip.
                resp.raise_for_status()

        # Fallback if API lookup fails to find a match (e.g. 404s all the way down)
        parts = full_path.split("/", 2)
        self.owner = parts[0]
        self.repo = parts[1] if len(parts) > 1 else ""
        self._project_id = f"{urllib.parse.quote(self.owner, safe='')}%2F{urllib.parse.quote(self.repo, safe='')}"
        if len(parts) > 2 and not self.path:
            self.path = parts[2]

    def build_manifest(self) -> list[ManifestEntry]:
        """Fetch the repo tree and build a manifest.

        Uses the recursive tree API. Blob IDs are content-addressable hashes.
        """
        self._ensure_resolved()
        ref = self.branch or self._get_default_branch()
        entries: list[ManifestEntry] = []

        # GitLab paginates the tree endpoint.
        page = 1
        while True:
            resp = self._http.get(
                f"/projects/{self._project_id}/repository/tree",
                params={
                    "ref": ref,
                    "recursive": "true",
                    "per_page": 100,
                    "page": page,
                    "path": self.path or "",
                },
            )
            resp.raise_for_status()
            items = resp.json()

            if not items:
                break

            for item in items:
                if item["type"] != "blob":
                    continue

                file_path = item["path"]

                # Strip prefix if scoped to a subdirectory.
                if self.path:
                    if not file_path.startswith(self.path + "/"):
                        continue
                    file_path = file_path[len(self.path) + 1 :]

                parts = file_path.rsplit("/", 1)
                if len(parts) == 2:
                    dir_path, filename = parts
                else:
                    dir_path, filename = "", parts[0]

                entries.append(
                    ManifestEntry(
                        filename=filename,
                        path=dir_path,
                        checksum=item["id"],  # Git blob SHA.
                        size=0,  # Tree endpoint doesn't return size.
                    )
                )

            page += 1

        entries.sort(key=lambda e: e.display_path)
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        """Download a file's raw content via the GitLab Repository Files API."""
        self._ensure_resolved()

        file_path = f"{path}/{filename}" if path else filename
        if self.path:
            file_path = f"{self.path}/{file_path}"

        encoded_path = urllib.parse.quote(file_path, safe="")
        ref = self.branch or self._get_default_branch()

        resp = self._http.get(
            f"/projects/{self._project_id}/repository/files/{encoded_path}/raw",
            params={"ref": ref},
        )
        resp.raise_for_status()
        return resp.content

    def _get_default_branch(self) -> str:
        """Fetch the project's default branch name."""
        self._ensure_resolved()
        resp = self._http.get(f"/projects/{self._project_id}")
        resp.raise_for_status()
        return resp.json()["default_branch"]

    def close(self) -> None:
        self._http.close()


def parse_gitlab_source(source: str) -> dict[str, str | None]:
    """Parse a gitlab:owner/repo[/path] source string.

    Supports:
      - Standard format: gitlab:owner/repo/path/to/docs
      - Explicit project:path format: gitlab:group/subgroup/project:path/to/docs
    """
    source = source.removeprefix("gitlab:")

    # Explicit separator project:path
    if ":" in source:
        project, path = source.split(":", 1)
        parts = project.rsplit("/", 1)
        if len(parts) == 2:
            return {"owner": parts[0], "repo": parts[1], "path": path}
        # Safe fallback if there is no slash in the project name
        return {"owner": project, "repo": None, "path": path}

    # Otherwise, return full path as owner and let connector resolve repo/path dynamically
    return {"owner": source, "repo": None, "path": None}
