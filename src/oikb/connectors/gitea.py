"""Gitea repository files and built-in Markdown wikis via REST API v1."""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import re
from urllib.parse import parse_qsl, quote, unquote, urlsplit, urlunsplit

import httpx

from oikb.connectors import BaseConnector, ManifestEntry

log = logging.getLogger(__name__)
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _relative_path(value: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\\" in value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Gitea paths must be relative paths without control characters")
    if empty and not value:
        return value
    if any(p in {"", ".", ".."} for p in value.split("/")):
        raise ValueError("Gitea paths must not contain empty or traversal components")
    return value


def resolve_gitea_url(base_url: str | None = None) -> str:
    """Resolve an instance root without making requests or inspecting credentials."""
    value = base_url if base_url is not None else os.environ.get("GITEA_URL", "")
    if not isinstance(value, str) or not value or any(c.isspace() for c in value) or "\\" in value:
        raise ValueError("Gitea instance URL required: set GITEA_URL or auth.base_url")
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or "?" in value or "#" in value):
        raise ValueError("Gitea URL must be an HTTP(S) instance root without credentials, query, or fragment")
    # Accessing port validates malformed port values too.
    port = parsed.port
    path = parsed.path.rstrip("/")
    if path:
        _relative_path(unquote(path.removeprefix("/")))
    if path.endswith("/api/v1"):
        raise ValueError("Use the Gitea instance root, without /api/v1")
    host = parsed.hostname.lower()
    host = f"[{host}]" if ":" in host else host
    if port is not None and port != (443 if parsed.scheme == "https" else 80):
        host += f":{port}"
    return urlunsplit((parsed.scheme, host, path, "", ""))


def parse_gitea_source(source: str) -> dict[str, str | bool | None]:
    if not source.startswith("gitea:"):
        raise ValueError("Expected gitea:owner/repo[/path][?wiki=true]")
    location, _, query = source.removeprefix("gitea:").partition("?")
    options = parse_qsl(query, keep_blank_values=True, strict_parsing=True)
    if len(options) > 1 or any(k != "wiki" or v not in {"true", "false"} for k, v in options):
        raise ValueError("Gitea supports only one wiki=true or wiki=false query option")
    parts = location.split("/", 2)
    if len(parts) < 2:
        raise ValueError("Expected gitea:owner/repo[/path]")
    owner, repo = parts[:2]
    for component in (owner, repo):
        _relative_path(component)
        if any(c in component for c in ":?#"):
            raise ValueError("Gitea source requires an owner and repository, not a clone URL")
    path = parts[2] if len(parts) == 3 else None
    if path is not None:
        path = _relative_path(path.rstrip("/"))
    wiki = bool(options and options[0][1] == "true")
    if wiki and path is not None:
        raise ValueError("Gitea wiki sources do not support branch or path options")
    return {"owner": owner, "repo": repo, "path": path, "is_wiki": wiki}


def _object_id(value: object) -> str:
    if not isinstance(value, str) or not _OID.fullmatch(value):
        raise ValueError("Invalid Gitea Git object ID")
    return value


def _integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("Invalid Gitea size or pagination count")
    return value


class GiteaConnector(BaseConnector):
    def __init__(
        self, owner: str, repo: str, branch: str | None = None,
        path: str | None = None, token: str | None = None,
        base_url: str | None = None, is_wiki: bool = False,
    ):
        for component in (owner, repo):
            _relative_path(component)
            if any(c in component for c in "/:?#"):
                raise ValueError("Gitea owner and repository must be single path components")
        if is_wiki and (branch is not None or path is not None):
            raise ValueError("Gitea wiki sources do not support branch or path options")
        if branch is not None:
            _relative_path(branch)
        self.owner, self.repo, self.branch = owner, repo, branch
        self.path = _relative_path(path.rstrip("/"), empty=True) if path is not None else ""
        self.is_wiki = is_wiki
        self._base_url = resolve_gitea_url(base_url)
        self._endpoint = f"repos/{quote(owner, safe='')}/{quote(repo, safe='')}"
        token = token if token is not None else os.environ.get("GITEA_TOKEN", "")
        if not isinstance(token, str) or any(ord(c) < 32 or ord(c) == 127 for c in token):
            raise ValueError("Gitea token must be a string without control characters")
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"token {token}"
        self._http = httpx.Client(
            base_url=self._base_url + "/api/v1/", headers=headers,
            timeout=60.0, follow_redirects=False,
        )
        self._commit: str | None = None
        self._files: dict[str, tuple[str, ManifestEntry]] = {}
        self._wiki: dict[str, bytes] = {}

    def _get(self, suffix: str = "", **kwargs) -> httpx.Response:
        response = self._http.get(self._endpoint + suffix, **kwargs)
        if response.is_redirect:
            raise ValueError("Gitea redirected the request; check the canonical instance URL and proxy configuration")
        response.raise_for_status()
        return response

    def build_manifest(self) -> list[ManifestEntry]:
        self._commit = None
        self._files.clear()
        self._wiki.clear()
        log.info("Scanning Gitea %s/%s/%s (%s)", self._base_url, self.owner, self.repo,
                 "wiki" if self.is_wiki else "files")
        metadata = self._get().json()
        if not isinstance(metadata, dict):
            raise ValueError("Invalid Gitea repository metadata")
        if self.is_wiki:
            if metadata.get("has_wiki") is not True or metadata.get("external_wiki"):
                raise ValueError("Gitea built-in wiki is disabled or external")
            return self._build_wiki_manifest()
        if type(metadata.get("empty")) is not bool:
            raise ValueError("Gitea repository metadata is missing the empty flag")
        if metadata["empty"]:
            if self.branch is not None or self.path:
                raise ValueError("Gitea repository is empty; requested branch or directory does not exist")
            return []
        branch = self.branch or metadata.get("default_branch")
        if not isinstance(branch, str) or not branch:
            raise ValueError("Gitea repository has no default branch")
        _relative_path(branch)
        commit = _object_id(self._get(f"/branches/{quote(branch, safe='')}").json()["commit"]["id"])
        log.info("Gitea %s/%s/%s tree at commit %s", self._base_url, self.owner, self.repo, commit)
        files: dict[str, tuple[str, ManifestEntry]] = {}
        seen: set[str] = set()
        page, total, tree_id = 1, None, None
        found_scope = not self.path
        while True:
            data = self._get(f"/git/trees/{commit}", params={
                "recursive": "true", "per_page": 1000, "page": page,
            }).json()
            if not isinstance(data, dict) or type(data.get("truncated")) is not bool:
                raise ValueError("Invalid Gitea tree pagination response")
            current_total = _integer(data.get("total_count"))
            current_id = _object_id(data.get("sha"))
            if _integer(data.get("page")) != page:
                raise ValueError("Gitea tree pagination did not advance")
            if total is None:
                total, tree_id = current_total, current_id
            if total != current_total or tree_id != current_id:
                raise ValueError("Gitea tree identity or pagination total changed")
            items = data.get("tree")
            if items is None and total == 0:
                items = []  # Gitea encodes an empty Git tree as null.
            if not isinstance(items, list) or (not items and data["truncated"]):
                raise ValueError("Incomplete Gitea tree pagination")
            for item in items:
                original = _relative_path(item["path"])
                if original in seen:
                    raise ValueError("Repeated Gitea tree path in pagination")
                seen.add(original)
                mode = item.get("mode")
                kinds = {"100644": "blob", "100755": "blob", "120000": "blob",
                         "040000": "tree", "160000": "commit"}
                if mode not in kinds or item.get("type") != kinds[mode]:
                    raise ValueError("Invalid Gitea tree entry mode or type")
                sha, size = _object_id(item.get("sha")), _integer(item.get("size"))
                if original == self.path and mode == "040000":
                    found_scope = True
                if self.path and not original.startswith(self.path + "/"):
                    continue
                found_scope = True
                if mode not in {"100644", "100755"}:
                    continue
                relative = original[len(self.path) + 1:] if self.path else original
                directory, _, filename = relative.rpartition("/")
                entry = ManifestEntry(filename, directory, sha, size)
                files[relative] = (original, entry)
            if len(seen) > total:
                raise ValueError("Contradictory Gitea tree pagination total")
            # Gitea 1.24 leaves truncated=true on the final populated page.
            # A verified stable total and unique paths establish completeness.
            if len(seen) == total:
                break
            if not data["truncated"]:
                raise ValueError("Incomplete Gitea tree pagination")
            page += 1
        if not found_scope:
            raise ValueError(f"Gitea directory does not exist: {self.path}")
        self._commit, self._files = commit, files
        return sorted((entry for _, entry in files.values()), key=lambda e: e.display_path)

    def _list_wiki(self) -> dict[str, str]:
        pages: dict[str, str] = {}
        page, limit, total = 1, 50, None
        while True:
            response = self._get("/wiki/pages", params={"page": page, "limit": limit})
            items = response.json()
            count = response.headers.get("x-total-count", "")
            if not count.isdecimal() or not isinstance(items, list):
                raise ValueError("Gitea wiki pagination requires a valid X-Total-Count and page list")
            if total is None:
                total = int(count)
            if total != int(count) or len(items) > limit:
                raise ValueError("Gitea wiki pagination total changed or is invalid")
            for item in items:
                name = item["sub_url"]
                _relative_path(name)
                if any(c in name for c in "/?#") or re.search(r"%(?![0-9A-Fa-f]{2})", name):
                    raise ValueError("Invalid Gitea wiki page identifier")
                if name in pages:
                    raise ValueError("Repeated Gitea wiki page in pagination")
                pages[name] = _object_id(item["last_commit"]["sha"])
            if len(pages) > total:
                raise ValueError("Contradictory Gitea wiki pagination total")
            next_url = response.links.get("next", {}).get("url")
            if next_url:
                pairs = parse_qsl(urlsplit(next_url).query, keep_blank_values=True)
                values = dict(pairs)
                if len(values) != len(pairs) or not values.get("page", "").isdecimal():
                    raise ValueError("Invalid Gitea wiki pagination link")
                next_page = int(values["page"])
                next_limit = values.get("limit", str(limit))
                if not next_limit.isdecimal() or int(next_limit) < 1 or next_page != page + 1:
                    raise ValueError("Gitea wiki pagination did not advance")
                effective_limit = int(next_limit)
                if (page > 1 and effective_limit != limit) or len(items) > effective_limit:
                    raise ValueError("Gitea wiki pagination limit changed")
                if page * effective_limit >= total:
                    raise ValueError("Contradictory Gitea wiki pagination link")
                page, limit = next_page, effective_limit
            elif page * limit < total:
                page += 1  # Older servers have a total count but no Link header.
            else:
                break
        return pages

    def _build_wiki_manifest(self) -> list[ManifestEntry]:
        revisions = self._list_wiki()
        content: dict[str, bytes] = {}
        entries = []
        for name, revision in revisions.items():
            data = self._get("/wiki/page/" + name).json()
            if data.get("sub_url") != name or data.get("last_commit", {}).get("sha") != revision:
                raise ValueError("Gitea wiki changed during scanning; retry the sync")
            encoded = data.get("content_base64")
            if not isinstance(encoded, str) or not encoded:
                raise ValueError(f"Gitea wiki page {name} is blank or exceeds the server blob limit; "
                                 "add content, remove the blank page, or increase DEFAULT_MAX_BLOB_SIZE")
            try:
                raw = base64.b64decode(encoded, validate=True)
            except (ValueError, UnicodeEncodeError) as exc:
                raise ValueError(f"Invalid Base64 content for Gitea wiki page {name}") from exc
            filename = name + ".md"
            content[filename] = raw
            entries.append(ManifestEntry(filename, "", hashlib.sha256(raw).hexdigest(), len(raw)))
        if self._list_wiki() != revisions:
            raise ValueError("Gitea wiki changed during scanning; retry the sync")
        self._wiki = content
        return sorted(entries, key=lambda e: e.display_path)

    def read_file(self, path: str, filename: str) -> bytes:
        key = f"{path}/{filename}" if path else filename
        if self.is_wiki:
            return self._wiki[key]
        original, entry = self._files[key]
        raw = self._get("/raw/" + quote(original, safe="/"), params={"ref": self._commit},
                        headers={"Accept": "application/octet-stream"}).content
        algorithm = "sha1" if len(entry.checksum) == 40 else "sha256"
        digest = hashlib.new(algorithm, b"blob " + str(len(raw)).encode() + b"\0" + raw).hexdigest()
        if len(raw) != entry.size or digest != entry.checksum:
            raise ValueError(f"Gitea file does not match the scanned Git object: {original}")
        return raw

    def close(self) -> None:
        self._http.close()
