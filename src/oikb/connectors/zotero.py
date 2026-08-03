# ---
# co_authored_by:
#   - @thiswillbeyourgithub
#     pull_request: https://github.com/open-webui/oikb/pull/61
# ---

"""Zotero connector - sync PDF attachment text from a Zotero library.

Auth via ZOTERO_LIBRARY_ID, ZOTERO_API_KEY, and optional ZOTERO_LIBRARY_TYPE.
Source syntax: zotero:<collection>%%<subcollection>. Use bare zotero: for all
top-level collections and unfiled library items.
"""

from __future__ import annotations

import html
import hashlib
import io
import os
import re
import tempfile
import zipfile
from dataclasses import dataclass
from typing import Any

import httpx

from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable

HIERARCHY_SEP = "%%"


@dataclass(frozen=True, slots=True)
class _ZoteroFile:
    item: dict[str, Any]
    attachment: dict[str, Any]
    notes: list[dict[str, Any]]
    annotations: list[dict[str, Any]]


class ZoteroConnector(BaseConnector):
    """Sync PDF attachments in Zotero collections as extracted .txt files."""

    def __init__(
        self,
        hierarchy: str | None = None,
        library_id: str | None = None,
        library_type: str | None = None,
        api_key: str | None = None,
    ):
        try:
            from pyzotero import zotero
        except ImportError as exc:
            raise ImportError(
                "Zotero connector requires pyzotero and pymupdf. "
                "Install with: pip install oikb[zotero]"
            ) from exc

        library_id = library_id or os.environ.get("ZOTERO_LIBRARY_ID", "")
        # Per-library env vars (e.g. ZOTERO_API_KEY_123456) allow multiple Zotero
        # libraries to be synced from a single daemon when the library_id is
        # embedded in the source string ("zotero:123456:Collection%%Sub").
        api_key = (
            api_key
            or (os.environ.get(f"ZOTERO_API_KEY_{library_id}") if library_id else None)
            or os.environ.get("ZOTERO_API_KEY", "")
        )
        if not library_id or not api_key:
            raise ValueError(
                "Zotero credentials required. Set ZOTERO_LIBRARY_ID and ZOTERO_API_KEY."
            )

        self.hierarchy = hierarchy
        self._zot = zotero.Zotero(
            library_id,
            library_type
            or (os.environ.get(f"ZOTERO_LIBRARY_TYPE_{library_id}") if library_id else None)
            or os.environ.get("ZOTERO_LIBRARY_TYPE", "user"),
            api_key,
        )
        self.include_notes = os.environ.get("ZOTERO_INCLUDE_NOTES", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        self.include_annotations = os.environ.get(
            "ZOTERO_INCLUDE_ANNOTATIONS", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        self.checksum_mode = os.environ.get("ZOTERO_CHECKSUM", "version").strip().lower()
        if self.checksum_mode not in {"version", "content"}:
            raise ValueError("ZOTERO_CHECKSUM must be 'version' or 'content'.")
        self.exclude = [
            part.strip().strip("/")
            for part in os.environ.get("ZOTERO_EXCLUDE", "").split(",")
            if part.strip().strip("/")
        ]
        self.unfiled_dir = os.environ.get("ZOTERO_UNFILED_DIR", "_unfiled").strip("/")
        self.webdav_url = os.environ.get("ZOTERO_WEBDAV_URL", "").rstrip("/")
        self.webdav_user = os.environ.get("ZOTERO_WEBDAV_USER", "")
        self.webdav_password = os.environ.get("ZOTERO_WEBDAV_PASSWORD", "")
        self._files: dict[tuple[str, str], _ZoteroFile] = {}
        self._text_cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        collections = self._zot.everything(self._zot.collections())
        roots = self._roots(collections)
        if self.hierarchy:
            root = self._find(collections, None, self.hierarchy.split(HIERARCHY_SEP))
            if root is None:
                raise ValueError(f"Zotero collection path not found: {self.hierarchy}")
            work = [(root, "")]
        else:
            work = [(root, root["name"]) for root in roots]

        entries: list[ManifestEntry] = []
        for root, path in work:
            rel_path = "" if self.hierarchy else root["name"]
            if rel_path and any(
                rel_path == item or rel_path.startswith(f"{item}/") for item in self.exclude
            ):
                continue
            self._walk_collection(root["key"], collections, path, entries, rel_path)

        if (
            not self.hierarchy
            and self.unfiled_dir
            and not any(
                self.unfiled_dir == item or self.unfiled_dir.startswith(f"{item}/")
                for item in self.exclude
            )
        ):
            for item in self._zot.everything(self._zot.items()):
                if (
                    item.get("data", {}).get("itemType")
                    not in {"attachment", "note", "annotation"}
                    and not item.get("data", {}).get("collections")
                ):
                    self._add_item(item, self.unfiled_dir, entries)

        entries.sort(key=lambda entry: entry.display_path)
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        file = self._files.get((path, filename))
        if not file:
            raise FileNotFoundError(f"Unknown Zotero file: {path}/{filename}")
        return self._file_text(file).encode("utf-8")

    def _walk_collection(
        self,
        collection_key: str,
        collections: list[dict[str, Any]],
        path: str,
        entries: list[ManifestEntry],
        rel_path: str,
    ) -> None:
        for item in self._zot.everything(self._zot.collection_items(collection_key)):
            self._add_item(item, path, entries)

        for child in collections:
            data = child.get("data", {})
            if data.get("parentCollection") != collection_key:
                continue
            child_path = f"{path}/{data['name']}" if path else data["name"]
            child_rel_path = f"{rel_path}/{data['name']}" if rel_path else data["name"]
            if any(
                child_rel_path == item or child_rel_path.startswith(f"{item}/")
                for item in self.exclude
            ):
                continue
            self._walk_collection(child["key"], collections, child_path, entries, child_rel_path)

    def _add_item(self, item: dict[str, Any], path: str, entries: list[ManifestEntry]) -> None:
        data = item.get("data", {})
        if data.get("itemType") in {"attachment", "note", "annotation"}:
            return

        children = self._zot.everything(self._zot.children(item["key"]))
        attachments = [
            child
            for child in children
            if self._is_pdf_attachment(child)
        ]
        notes = [
            child for child in children if child.get("data", {}).get("itemType") == "note"
        ] if self.include_notes else []
        parent_annotations = [
            child
            for child in children
            if child.get("data", {}).get("itemType") == "annotation"
        ] if self.include_annotations else []
        for index, attachment in enumerate(attachments):
            filename = self._unique_filename(
                path,
                self._filename(data.get("title", "Untitled"), index),
            )
            annotations = parent_annotations
            if self.include_annotations:
                annotations = [
                    *parent_annotations,
                    *[
                        child
                        for child in self._zot.everything(self._zot.children(attachment["key"]))
                        if child.get("data", {}).get("itemType") == "annotation"
                    ],
                ]
            file = _ZoteroFile(item, attachment, notes, annotations)
            self._files[(path, filename)] = file
            entries.append(
                ManifestEntry(
                    filename=filename,
                    path=path,
                    checksum=self._checksum(file),
                    size=0,
                )
            )

    @staticmethod
    def _roots(collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {"key": item["key"], "name": item["data"]["name"]}
            for item in collections
            if not item.get("data", {}).get("parentCollection")
        ]

    def _find(
        self,
        collections: list[dict[str, Any]],
        parent_key: str | None,
        parts: list[str],
    ) -> dict[str, Any] | None:
        if not parts:
            return None
        for item in collections:
            data = item.get("data", {})
            parent = data.get("parentCollection")
            if parent != parent_key or data.get("name") != parts[0]:
                continue
            if len(parts) == 1:
                return {"key": item["key"], "name": data["name"]}
            return self._find(collections, item["key"], parts[1:])
        return None

    @staticmethod
    def _is_pdf_attachment(item: dict[str, Any]) -> bool:
        data = item.get("data", {})
        if data.get("itemType") != "attachment":
            return False
        content_type = data.get("contentType", "")
        title = data.get("title", "")
        path = data.get("path", "")
        return (
            content_type == "application/pdf"
            or title.lower().endswith(".pdf")
            or path.lower().endswith(".pdf")
        )

    @staticmethod
    def _filename(title: str, index: int) -> str:
        name = re.sub(r"<[^>]+>", "", title)
        name = re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "Untitled"
        suffix = f"_{index + 1}" if index else ""
        return f"{name[:180]}{suffix}.txt"

    def _unique_filename(self, path: str, filename: str) -> str:
        if (path, filename) not in self._files:
            return filename
        stem, _, ext = filename.rpartition(".")
        index = 2
        while (path, f"{stem}_{index}.{ext}") in self._files:
            index += 1
        return f"{stem}_{index}.{ext}"

    def _checksum(self, file: _ZoteroFile) -> str:
        if self.checksum_mode == "content":
            try:
                return hashlib.sha256(self._file_text(file).encode()).hexdigest()
            except SourceFileUnavailable:
                pass

        parts = [
            f"{item.get('key')}:{item.get('version', item.get('data', {}).get('version', 0))}"
            for item in [file.item, file.attachment, *file.notes, *file.annotations]
        ]
        raw = ":".join([self.checksum_mode, *parts])
        return hashlib.sha256(raw.encode()).hexdigest()

    def _file_text(self, file: _ZoteroFile) -> str:
        parts = [self._attachment_text(file.attachment["key"])]
        if self.include_notes:
            notes = [
                html.unescape(
                    re.sub(r"<[^>]+>", "", note.get("data", {}).get("note", "") or "")
                ).strip()
                for note in file.notes
            ]
            notes = [note for note in notes if note]
            if notes:
                parts.append("Zotero notes\n\n" + "\n\n".join(notes))
        if self.include_annotations:
            annotations = []
            for item in file.annotations:
                data = item.get("data", {})
                annotation_parts = []
                if data.get("annotationPageLabel"):
                    annotation_parts.append(f"Page {data['annotationPageLabel']}")
                if data.get("annotationText"):
                    annotation_parts.append(
                        html.unescape(
                            re.sub(r"<[^>]+>", "", data["annotationText"] or "")
                        ).strip()
                    )
                if data.get("annotationComment"):
                    annotation_parts.append(
                        html.unescape(
                            re.sub(r"<[^>]+>", "", data["annotationComment"] or "")
                        ).strip()
                    )
                annotations.append("\n".join(part for part in annotation_parts if part))
            annotations = [annotation for annotation in annotations if annotation]
            if annotations:
                parts.append("Zotero annotations\n\n" + "\n\n".join(annotations))
        return "\n\n".join(part for part in parts if part)

    def _attachment_text(self, attachment_key: str) -> str:
        if attachment_key in self._text_cache:
            return self._text_cache[attachment_key]

        try:
            text = self._zot.fulltext_item(attachment_key).get("content", "")
            if text:
                self._text_cache[attachment_key] = text
                return text
        except Exception:
            pass

        try:
            pdf_bytes = self._zot.file(attachment_key)
        except Exception as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) != 404 and "404" not in str(exc):
                raise
            if not self.webdav_url:
                raise SourceFileUnavailable(
                    f"Zotero has no downloadable file for attachment {attachment_key}"
                ) from exc
            pdf_bytes = self._webdav_file(attachment_key)

        return self._extract_pdf_text(attachment_key, pdf_bytes)

    def _extract_pdf_text(self, attachment_key: str, pdf_bytes: bytes) -> str:
        try:
            import fitz
        except ImportError as exc:
            raise ImportError(
                "pymupdf is required for Zotero PDF extraction. "
                "Install with: pip install oikb[zotero]"
            ) from exc

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        try:
            doc = fitz.open(tmp_path)
            text = "".join(page.get_text() for page in doc)
            doc.close()
            self._text_cache[attachment_key] = text
            return text
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    def _webdav_file(self, attachment_key: str) -> bytes:
        auth = None
        if self.webdav_user or self.webdav_password:
            auth = (self.webdav_user, self.webdav_password)
        url = f"{self.webdav_url}/{attachment_key}.zip"
        response = httpx.get(url, auth=auth, timeout=120.0)
        if response.status_code == 404:
            raise SourceFileUnavailable(
                f"Zotero/WebDAV have no downloadable file for attachment {attachment_key}"
            )
        response.raise_for_status()
        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                for name in archive.namelist():
                    if name.lower().endswith(".pdf"):
                        return archive.read(name)
        except zipfile.BadZipFile:
            if response.content.startswith(b"%PDF"):
                return response.content
            raise
        raise SourceFileUnavailable(
            f"WebDAV archive for attachment {attachment_key} does not contain a PDF"
        )


def parse_zotero_source(source: str) -> dict[str, str | None]:
    rest = source.removeprefix("zotero:")
    library_id: str | None = None
    # Optional library ID prefix: "zotero:123456:Collection%%Sub". A numeric
    # segment before the first colon is treated as the library ID, allowing
    # multiple Zotero libraries in a single .oikb.yaml. Plain collection paths
    # (e.g. "zotero:Research%%ML") are unchanged — collection names are not
    # purely numeric, so there is no ambiguity.
    if ":" in rest:
        head, tail = rest.split(":", 1)
        if head.isdigit():
            library_id = head
            rest = tail
    return {"hierarchy": None if rest in {"", "*"} else rest, "library_id": library_id}
