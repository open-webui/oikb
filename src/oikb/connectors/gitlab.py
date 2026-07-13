"""GitLab connector — sync a GitLab repo (or its project wiki) to a KB via the API.

Uses the GitLab Repository Tree API for repositories and the Project Wikis API
for wikis — no local clone needed. Set GITLAB_TOKEN (and GITLAB_URL for a
self-managed instance).
"""

from __future__ import annotations

import hashlib
import os
import urllib.parse

import httpx

from oikb.connectors import BaseConnector, ManifestEntry

# GitLab wiki markup formats → file extension (wiki mode only).
_WIKI_FORMAT_EXT = {
    "markdown": ".md",
    "rdoc": ".rdoc",
    "asciidoc": ".adoc",
    "org": ".org",
}


class GitLabConnector(BaseConnector):
    """Sync files from a GitLab repository.

    Args:
        owner:      Project namespace (e.g. "open-webui"). Omit if project_id
                    is given.
        repo:       Project name (e.g. "docs"). Omit if project_id is given.
        branch:     Branch to sync from (default: project default branch).
        path:       Subdirectory to scope to (e.g. "docs/").
        token:      GitLab personal access token (or GITLAB_TOKEN env var).
        base_url:   GitLab instance URL (default: https://gitlab.com).
        is_wiki:    Sync the project wiki instead of the repository. In wiki
                    mode ``branch`` and ``path`` are ignored (default: False).
        project_id: Explicit GitLab project ID (numeric) or pre-encoded path,
                    used verbatim instead of owner/repo.
    """

    def __init__(
        self,
        owner: str | None = None,
        repo: str | None = None,
        branch: str | None = None,
        path: str | None = None,
        token: str | None = None,
        base_url: str | None = None,
        is_wiki: bool = False,
        project_id: str | None = None,
    ):
        self.owner = owner
        self.repo = repo
        self.branch = branch
        self.path = path.strip("/") if path else None
        self.is_wiki = is_wiki
        # Populated by build_manifest() in wiki mode; read_file() serves from it.
        self._cache: dict[str, str] = {}
        self._token = token or os.environ.get("GITLAB_TOKEN")
        self._base_url = (base_url or os.environ.get("GITLAB_URL", "https://gitlab.com")).rstrip("/")

        headers: dict[str, str] = {}
        if self._token:
            headers["PRIVATE-TOKEN"] = self._token

        self._http = httpx.Client(
            base_url=f"{self._base_url}/api/v4",
            headers=headers,
            timeout=60.0,
        )

        # GitLab's :id accepts a numeric project ID or the URL-encoded project
        # path. An explicit project_id (e.g. "42") is used as-is and avoids any
        # namespace/subgroup guesswork; otherwise encode the whole
        # "namespace/project" so subgroup paths stay correct.
        if project_id is not None:
            self._project_id = str(project_id)
        else:
            self._project_id = urllib.parse.quote(f"{self.owner}/{self.repo}", safe="")

    def build_manifest(self) -> list[ManifestEntry]:
        """Fetch the repo tree (or wiki pages) and build a manifest.

        Repo mode uses the recursive tree API; blob IDs are content-addressable
        hashes. Wiki mode uses the Project Wikis API.
        """
        if self.is_wiki:
            return self._build_wiki_manifest()

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
        """Return raw file content (repo) or the cached wiki page body."""
        if self.is_wiki:
            key = f"{path}/{filename}" if path else filename
            return (self._cache.get(key) or "").encode("utf-8")

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

    def _build_wiki_manifest(self) -> list[ManifestEntry]:
        """Fetch every wiki page and build a manifest.

        The list endpoint returns all pages; with_content=1 includes each body
        in a single request on GitLab 16.4+. Older instances silently ignore the
        flag, so any page returned without content is fetched individually via
        GET /wikis/:slug.
        """
        resp = self._http.get(
            f"/projects/{self._project_id}/wikis",
            params={"with_content": 1},
        )
        resp.raise_for_status()

        entries: list[ManifestEntry] = []
        for page in resp.json():
            slug = page.get("slug")
            if not slug:
                continue

            content = page.get("content")
            if content is None:  # older GitLab ignored with_content — fetch it.
                content = self._fetch_wiki_page(slug)

            title = page.get("title") or slug
            text = f"# {title}\n\n{content or ''}"

            ext = _WIKI_FORMAT_EXT.get(page.get("format", "markdown"), ".md")
            if "/" in slug:  # Wiki slugs are hierarchical, e.g. "api/auth".
                dir_path, base = slug.rsplit("/", 1)
            else:
                dir_path, base = "", slug

            entry = ManifestEntry(
                filename=f"{base}{ext}",
                path=dir_path,
                checksum=hashlib.sha256(text.encode()).hexdigest()[:16],
                size=len(text.encode()),
            )
            entries.append(entry)
            # Key by full display path — two pages can share a basename
            # (api/index vs guide/index).
            self._cache[entry.display_path] = text

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _fetch_wiki_page(self, slug: str) -> str:
        """Fetch one wiki page's content by slug (GET /wikis/:slug)."""
        encoded = urllib.parse.quote(slug, safe="")
        resp = self._http.get(f"/projects/{self._project_id}/wikis/{encoded}")
        resp.raise_for_status()
        return resp.json().get("content") or ""

    def _get_default_branch(self) -> str:
        """Fetch the project's default branch name."""
        resp = self._http.get(f"/projects/{self._project_id}")
        resp.raise_for_status()
        return resp.json()["default_branch"]

    def close(self) -> None:
        self._http.close()


def parse_gitlab_source(source: str) -> dict[str, str | bool | None]:
    """Parse a gitlab source string.

    Forms:
        gitlab:owner/repo                  # repository
        gitlab:owner/repo/subdir           # repository, scoped to a subdirectory
        gitlab:owner/repo?wiki=true         # project wiki
        gitlab:group/subgroup/proj?wiki=true  # wiki of a subgroup project
        gitlab:42                          # repository by numeric project ID
        gitlab:42?wiki=true                # wiki by numeric project ID
    """
    raw = source.removeprefix("gitlab:")
    base, _, query = raw.partition("?")

    is_wiki = False
    for param in query.split("&"):
        key, _, value = param.partition("=")
        if key == "wiki":
            is_wiki = value.lower() in ("", "1", "true", "yes")

    # A bare numeric project ID is used directly as GitLab's :id — handy when
    # the namespace path is deep, awkward to encode, or simply unknown.
    if base.isdigit():
        return {"owner": None, "repo": None, "path": None, "project_id": base, "wiki": is_wiki}

    if "/" not in base:
        raise ValueError(f"Invalid GitLab source: {source}. Expected: gitlab:owner/repo")

    if is_wiki:
        # Wikis have no subdirectory concept, and self-managed projects often
        # live under nested subgroups (group/subgroup/project) — so treat the
        # entire path as the project: everything up to the last "/" is the
        # namespace, the final segment is the project.
        owner, repo = base.rsplit("/", 1)
        return {"owner": owner, "repo": repo, "path": None, "project_id": None, "wiki": True}

    parts = base.split("/", 2)
    owner = parts[0]
    repo = parts[1]
    path = parts[2] if len(parts) > 2 else None
    return {"owner": owner, "repo": repo, "path": path, "project_id": None, "wiki": False}