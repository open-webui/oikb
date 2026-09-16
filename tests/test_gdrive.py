from __future__ import annotations

from typing import Any

from oikb.connectors.gdrive import GDriveConnector


class _Request:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def execute(self) -> dict[str, Any]:
        return self.payload


class _FilesResource:
    def __init__(self, listings: dict[str, list[dict[str, str]]]) -> None:
        self.listings = listings
        self.queried_folders: list[str] = []

    def list(self, **kwargs: Any) -> _Request:
        parent_id = kwargs["q"].split("'", 2)[1]
        self.queried_folders.append(parent_id)
        return _Request({"files": self.listings[parent_id]})


class _DriveService:
    def __init__(self, listings: dict[str, list[dict[str, str]]]) -> None:
        self.files_resource = _FilesResource(listings)

    def files(self) -> _FilesResource:
        return self.files_resource


def test_build_manifest_recurses_into_subfolders() -> None:
    service = _DriveService(
        {
            "root-folder": [
                {
                    "id": "root-file",
                    "name": "root.txt",
                    "mimeType": "text/plain",
                    "md5Checksum": "root-checksum",
                    "size": "4",
                },
                {
                    "id": "nested-folder",
                    "name": "nested",
                    "mimeType": "application/vnd.google-apps.folder",
                },
            ],
            "nested-folder": [
                {
                    "id": "nested-file",
                    "name": "child.txt",
                    "mimeType": "text/plain",
                    "md5Checksum": "child-checksum",
                    "size": "5",
                }
            ],
        }
    )
    connector = GDriveConnector.__new__(GDriveConnector)
    connector.folder_id = "root-folder"
    connector._service = service

    manifest = connector.build_manifest()

    assert [entry.display_path for entry in manifest] == [
        "nested/child.txt",
        "root.txt",
    ]
    assert service.files_resource.queried_folders == [
        "root-folder",
        "nested-folder",
    ]
