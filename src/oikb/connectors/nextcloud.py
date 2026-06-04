"""Nextcloud connector - sync a Nextcloud folder to a Knowledge Base.

Auth via NEXTCLOUD_URL, NEXTCLOUD_USER, NEXTCLOUD_PASSWORD env vars.
NEXTCLOUD_PASSWORD can be a regular password or, preferably, an app password.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

import httpx

from oikb.connectors import BaseConnector, ManifestEntry

DAV_NS = {"d": "DAV:", "oc": "http://owncloud.org/ns"}
PROPFIND_BODY = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:prop>
    <d:resourcetype/>
    <d:getetag/>
    <d:getcontenttype/>
    <d:getcontentlength/>
    <d:getlastmodified/>
    <oc:fileid/>
  </d:prop>
</d:propfind>
"""


@dataclass(frozen=True)
class _DavEntry:
    path: str
    file_id: str | None
    etag: str | None
    size: int
    modified_at: str | None
    is_collection: bool


class _WebDAVClient:
    def __init__(self, root_url: str, http: httpx.Client):
        self.root_url = root_url.rstrip("/")
        self._http = http

    def walk(self, root: str) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        normalized_root = _normalize_path(root)
        self._walk_folder(normalized_root, normalized_root, entries, set())
        entries.sort(key=lambda e: e.display_path)
        return entries

    def read_file(self, root: str, path: str, filename: str) -> bytes:
        file_path = _join_paths(root, path, filename)
        resp = self._request("GET", self._url(file_path), ok_statuses={200})
        return resp.content

    def _walk_folder(
        self,
        root: str,
        folder_path: str,
        entries: list[ManifestEntry],
        seen_dirs: set[str],
    ) -> None:
        folder_path = _normalize_path(folder_path)
        if folder_path in seen_dirs:
            return
        seen_dirs.add(folder_path)

        for entry in self._propfind(folder_path):
            if entry.path == folder_path:
                continue
            if entry.is_collection:
                self._walk_folder(root, entry.path, entries, seen_dirs)
                continue

            relative_path = _relative_to_root(root, entry.path)
            if relative_path is None:
                continue

            dir_path, filename = _split_relative_file(relative_path)
            entries.append(
                ManifestEntry(
                    filename=filename,
                    path=dir_path,
                    checksum=_checksum(entry),
                    size=entry.size,
                )
            )

    def _propfind(self, path: str) -> list[_DavEntry]:
        resp = self._request(
            "PROPFIND",
            self._url(path),
            ok_statuses={200, 207},
            data=PROPFIND_BODY,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        return self._parse_multistatus(resp.text)

    def _parse_multistatus(self, xml_text: str) -> list[_DavEntry]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as exc:
            raise ValueError("Invalid WebDAV XML response") from exc

        entries: list[_DavEntry] = []
        for response in root.findall("d:response", DAV_NS):
            href = response.findtext("d:href", default="", namespaces=DAV_NS)
            path = self._href_to_path(href)
            prop = response.find("d:propstat/d:prop", DAV_NS)
            if not path or prop is None:
                continue

            resource_type = prop.find("d:resourcetype", DAV_NS)
            is_collection = (
                resource_type is not None
                and resource_type.find("d:collection", DAV_NS) is not None
            )
            raw_size = prop.findtext("d:getcontentlength", default="", namespaces=DAV_NS)
            size = int(raw_size.strip()) if raw_size.strip().isdigit() else 0

            entries.append(
                _DavEntry(
                    path=path,
                    file_id=_empty_to_none(
                        prop.findtext("oc:fileid", default="", namespaces=DAV_NS)
                    ),
                    etag=_clean_etag(
                        prop.findtext("d:getetag", default="", namespaces=DAV_NS)
                    ),
                    size=size,
                    modified_at=_empty_to_none(
                        prop.findtext(
                            "d:getlastmodified",
                            default="",
                            namespaces=DAV_NS,
                        )
                    ),
                    is_collection=is_collection,
                )
            )
        return entries

    def _href_to_path(self, href: str) -> str | None:
        dav_root = urlsplit(self._url("/")).path.rstrip("/")
        raw_path = urlsplit(href).path
        if not raw_path.startswith(dav_root):
            return None
        suffix = unquote(raw_path[len(dav_root) :]) or "/"
        return _normalize_path(suffix)

    def _url(self, path: str) -> str:
        segments = [
            quote(segment, safe="")
            for segment in _normalize_path(path).strip("/").split("/")
            if segment
        ]
        if not segments:
            return self.root_url
        return f"{self.root_url}/{'/'.join(segments)}"

    def _request(self, method: str, url: str, *, ok_statuses: set[int], **kwargs):
        resp = self._http.request(method, url, **kwargs)
        if resp.status_code not in ok_statuses:
            raise httpx.HTTPStatusError(
                f"{method} {url} failed: HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        return resp


class NextcloudConnector(BaseConnector):
    """Sync files from a Nextcloud folder via WebDAV.

    Args:
        root:     Folder path inside the authenticated Nextcloud user's files.
        base_url: Nextcloud base URL, or NEXTCLOUD_URL env var.
        user:     Nextcloud username, or NEXTCLOUD_USER env var.
        password: Nextcloud password/app password, or NEXTCLOUD_PASSWORD env var.
    """

    def __init__(
        self,
        root: str,
        base_url: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        self.root = _normalize_path(root)
        self._base_url = (base_url or os.environ.get("NEXTCLOUD_URL", "")).rstrip("/")
        self._user = user or os.environ.get("NEXTCLOUD_USER", "")
        self._password = password or os.environ.get("NEXTCLOUD_PASSWORD", "")
        self._dav_user_id: str | None = None
        self._webdav: _WebDAVClient | None = None

        if not self._base_url:
            raise ValueError("Nextcloud URL required. Set NEXTCLOUD_URL env var.")
        if not self._user or not self._password:
            raise ValueError(
                "Nextcloud credentials required. Set NEXTCLOUD_USER and "
                "NEXTCLOUD_PASSWORD env vars."
            )

        self._http = httpx.Client(
            auth=(self._user, self._password),
            timeout=60.0,
            headers={"User-Agent": "oikb/nextcloud"},
        )

    def build_manifest(self) -> list[ManifestEntry]:
        """Recursively scan the configured Nextcloud root."""
        return self._get_webdav().walk(self.root)

    def read_file(self, path: str, filename: str) -> bytes:
        """Download a file from Nextcloud."""
        return self._get_webdav().read_file(self.root, path, filename)

    def close(self) -> None:
        self._http.close()

    def _get_webdav(self) -> _WebDAVClient:
        if self._webdav is None:
            self._webdav = _WebDAVClient(
                root_url=self._dav_root_url(),
                http=self._http,
            )
        return self._webdav

    def _dav_root_url(self) -> str:
        return (
            f"{self._base_url}/remote.php/dav/files/"
            f"{quote(self._resolve_dav_user_id(), safe='')}"
        )

    def _resolve_dav_user_id(self) -> str:
        if self._dav_user_id:
            return self._dav_user_id

        resp = self._request(
            "GET",
            f"{self._base_url}/ocs/v2.php/cloud/user?format=json",
            ok_statuses={200},
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        payload = resp.json()
        dav_user_id = payload.get("ocs", {}).get("data", {}).get("id")
        if not dav_user_id:
            raise ValueError("Nextcloud OCS user payload missing DAV user id")
        self._dav_user_id = str(dav_user_id)
        return self._dav_user_id

    def _request(self, method: str, url: str, *, ok_statuses: set[int], **kwargs):
        resp = self._http.request(method, url, **kwargs)
        if resp.status_code not in ok_statuses:
            raise httpx.HTTPStatusError(
                f"{method} {url} failed: HTTP {resp.status_code}",
                request=resp.request,
                response=resp,
            )
        return resp


def parse_nextcloud_source(source: str) -> dict[str, str | None]:
    """Parse a nextcloud:/path source string."""
    root = source.removeprefix("nextcloud:")
    if not root:
        root = "/"
    return {"root": _normalize_path(root)}


def _normalize_path(path: str) -> str:
    normalized = (path or "").strip()
    if not normalized:
        normalized = "/"
    if not normalized.startswith("/"):
        normalized = f"/{normalized}"
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    return normalized


def _split_relative_file(relative_path: str) -> tuple[str, str]:
    parts = relative_path.strip("/").rsplit("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return "", parts[0]


def _join_paths(*parts: str) -> str:
    joined = "/".join(part.strip("/") for part in parts if part and part.strip("/"))
    return _normalize_path(joined)


def _relative_to_root(root: str, path: str) -> str | None:
    normalized_root = _normalize_path(root)
    normalized_path = _normalize_path(path)
    if normalized_root == "/":
        return normalized_path.strip("/")
    if normalized_path == normalized_root:
        return ""
    prefix = f"{normalized_root}/"
    if not normalized_path.startswith(prefix):
        return None
    return normalized_path[len(prefix) :]


def _empty_to_none(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _clean_etag(value: str | None) -> str | None:
    text = (value or "").strip().strip('"')
    return text or None


def _checksum(entry: _DavEntry) -> str:
    if entry.etag:
        return entry.etag
    fallback = f"{entry.file_id or ''}:{entry.modified_at or ''}:{entry.size}"
    return hashlib.sha256(fallback.encode()).hexdigest()
