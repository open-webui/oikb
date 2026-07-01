"""Google Drive connector — sync a Drive folder to a Knowledge Base.

Requires: pip install oikb[gdrive]
Exports Google Docs as markdown, Sheets as CSV, Slides as text.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from oikb.connectors import BaseConnector, ManifestEntry

# Google Docs MIME types that need export.
_EXPORT_MIMES: dict[str, tuple[str, str]] = {
    "application/vnd.google-apps.document": ("text/plain", ".txt"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", ".csv"),
    "application/vnd.google-apps.presentation": ("text/plain", ".txt"),
}

# MIME types to skip (folders, forms, maps, etc.).
_SKIP_MIMES = frozenset({
    "application/vnd.google-apps.folder",
    "application/vnd.google-apps.form",
    "application/vnd.google-apps.map",
    "application/vnd.google-apps.site",
    "application/vnd.google-apps.shortcut",
})


class GDriveConnector(BaseConnector):
    """Sync files from a Google Drive folder.

    Args:
        folder_id:            Drive folder ID.
        service_account_file: Path to service account JSON
                              (or GOOGLE_APPLICATION_CREDENTIALS env var).
    """

    def __init__(
        self,
        folder_id: str,
        service_account_file: str | None = None,
    ):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError:
            raise ImportError(
                "Google Drive connector requires google-api-python-client and google-auth. "
                "Install with: pip install oikb[gdrive]"
            )

        self.folder_id = folder_id

        creds_file = service_account_file or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        if not creds_file:
            raise ValueError(
                "Google service account credentials required. Set via:\n"
                "  export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json"
            )

        creds = service_account.Credentials.from_service_account_file(
            creds_file,
            scopes=["https://www.googleapis.com/auth/drive.readonly"],
        )
        self._service = build("drive", "v3", credentials=creds)

    def build_manifest(self) -> list[ManifestEntry]:
        """List all files in the folder recursively."""
        entries: list[ManifestEntry] = []
        self._walk_folder(self.folder_id, "", entries)
        entries.sort(key=lambda e: e.display_path)
        return entries

    def _walk_folder(
        self,
        folder_id: str,
        relative_prefix: str,
        entries: list[ManifestEntry],
    ) -> None:
        """Recursively list files in a Drive folder."""
        page_token = None

        while True:
            resp = (
                self._service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, md5Checksum, modifiedTime, size)",
                    pageSize=1000,
                    pageToken=page_token,
                    supportsAllDrives=True,
                    includeItemsFromAllDrives=True,
                )
                .execute()
            )

            for item in resp.get("files", []):
                mime = item["mimeType"]

                if mime in _SKIP_MIMES:
                    continue

                if mime == "application/vnd.google-apps.folder":
                    # Recurse into subfolders.
                    sub_prefix = f"{relative_prefix}/{item['name']}" if relative_prefix else item["name"]
                    self._walk_folder(item["id"], sub_prefix, entries)
                    continue

                # Determine filename (add extension for exported types).
                filename = item["name"]
                if mime in _EXPORT_MIMES:
                    _, ext = _EXPORT_MIMES[mime]
                    if not filename.endswith(ext):
                        filename += ext

                # Use md5 checksum for native files, hash modifiedTime for Google Docs.
                checksum = item.get("md5Checksum") or hashlib.sha256(
                    item.get("modifiedTime", "").encode()
                ).hexdigest()[:16]

                entries.append(
                    ManifestEntry(
                        filename=filename,
                        path=relative_prefix,
                        checksum=checksum,
                        size=int(item.get("size", 0)),
                    )
                )

            page_token = resp.get("nextPageToken")
            if not page_token:
                break

    def read_file(self, path: str, filename: str) -> bytes:
        """Download or export a file from Drive."""
        # Find the file by walking to it.
        file_id = self._find_file(path, filename)
        if not file_id:
            raise FileNotFoundError(f"File not found in Drive: {path}/{filename}")

        # Check if it needs export.
        meta = (
            self._service.files()
            .get(fileId=file_id, fields="mimeType", supportsAllDrives=True)
            .execute()
        )
        mime = meta["mimeType"]

        if mime in _EXPORT_MIMES:
            export_mime, _ = _EXPORT_MIMES[mime]
            return (
                self._service.files()
                .export(fileId=file_id, mimeType=export_mime)
                .execute()
            )

        return (
            self._service.files()
            .get_media(fileId=file_id, supportsAllDrives=True)
            .execute()
        )

    def _find_file(self, path: str, filename: str) -> str | None:
        """Find a file ID by navigating the folder path."""
        # Strip export extension for Google Docs lookup.
        search_name = filename
        for _, ext in _EXPORT_MIMES.values():
            if filename.endswith(ext):
                search_name = filename[: -len(ext)]
                break

        current_folder = self.folder_id

        # Navigate to the target directory.
        if path:
            for segment in path.split("/"):
                resp = (
                    self._service.files()
                    .list(
                        q=f"'{current_folder}' in parents and name = '{segment}' and mimeType = 'application/vnd.google-apps.folder' and trashed = false",
                        fields="files(id)",
                        supportsAllDrives=True,
                        includeItemsFromAllDrives=True,
                    )
                    .execute()
                )
                files = resp.get("files", [])
                if not files:
                    return None
                current_folder = files[0]["id"]

        # Find the file.
        resp = (
            self._service.files()
            .list(
                q=f"'{current_folder}' in parents and name = '{search_name}' and trashed = false",
                fields="files(id)",
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
            )
            .execute()
        )
        files = resp.get("files", [])
        return files[0]["id"] if files else None


def parse_gdrive_source(source: str) -> dict[str, str | None]:
    """Parse a gdrive:folder-id source string."""
    folder_id = source.removeprefix("gdrive:")
    if not folder_id:
        raise ValueError("Invalid Google Drive source. Expected: gdrive:<folder-id>")
    return {"folder_id": folder_id}
