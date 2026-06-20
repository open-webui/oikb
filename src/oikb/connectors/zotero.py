"""Zotero connector — sync PDF text from a Zotero library to a Knowledge Base.

Extracts text from the PDF attachments of items in a Zotero collection (and its
subcollections) and uploads them as .txt files. The collection hierarchy maps to
Knowledge Base directories.

This connector is READ-ONLY with respect to Zotero: it never modifies, deletes, or
adds anything to your Zotero library.

Auth and options via env vars:
  ZOTERO_LIBRARY_ID    Zotero library id (required)
  ZOTERO_LIBRARY_TYPE  'user' or 'group' (default: user)
  ZOTERO_API_KEY       Zotero API key (required)
  ZOTERO_CHECKSUM      change-detection mode (default: version):
                         'version' — cheap, hashes the Zotero item version (no download)
                         'content' — hashes the extracted text (accurate, downloads all)
  ZOTERO_EXCLUDE       ';'-separated collection paths to skip, relative to the synced
                       root and using %% as separator. When syncing
                       "zotero:Research", exclude its subcollections as "Archive;Drafts".

Source syntax: zotero:<hierarchy> where hierarchy uses %% as the separator, e.g.
  zotero:Research%%Machine Learning
An empty hierarchy (just "zotero:") syncs every top-level collection.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile

from oikb.connectors import BaseConnector, ManifestEntry

# Separator between collection names in a source hierarchy string.
HIERARCHY_SEP = "%%"
# Max filename length to stay well under filesystem / API limits.
MAX_FILENAME = 200


class ZoteroConnector(BaseConnector):
    """Sync PDF text from a Zotero library to a Knowledge Base."""

    def __init__(
        self,
        hierarchy: str | None = None,
        library_id: str | None = None,
        library_type: str | None = None,
        api_key: str | None = None,
        checksum: str | None = None,
        exclude: str | None = None,
    ):
        self.hierarchy = hierarchy
        lib_id = library_id or os.environ.get("ZOTERO_LIBRARY_ID", "")
        lib_type = library_type or os.environ.get("ZOTERO_LIBRARY_TYPE", "user")
        key = api_key or os.environ.get("ZOTERO_API_KEY", "")
        if not lib_id or not key:
            raise ValueError(
                "Zotero credentials required. Set ZOTERO_LIBRARY_ID and ZOTERO_API_KEY."
            )

        self.checksum_mode = (checksum or os.environ.get("ZOTERO_CHECKSUM", "version")).lower()
        if self.checksum_mode not in ("version", "content"):
            raise ValueError(
                f"Invalid ZOTERO_CHECKSUM '{self.checksum_mode}'. Use 'version' or 'content'."
            )

        exclude_raw = exclude if exclude is not None else os.environ.get("ZOTERO_EXCLUDE", "")
        self.excluded_paths = {p.strip() for p in exclude_raw.split(";") if p.strip()}

        try:
            from pyzotero import zotero
        except ImportError as e:
            raise ImportError(
                "pyzotero is required for the Zotero connector. Install with: pip install oikb[zotero]"
            ) from e
        self._zot = zotero.Zotero(lib_id, lib_type, key)

        # (path, filename) -> attachment key, populated during build_manifest.
        self._index: dict[tuple[str, str], str] = {}
        # attachment key -> extracted text, so read_file (and content-mode
        # checksumming) never extracts the same attachment twice.
        self._text_cache: dict[str, str] = {}

    # ── manifest ────────────────────────────────────────────────

    def build_manifest(self) -> list[ManifestEntry]:
        all_collections = self._zot.everything(self._zot.collections())
        tree = self._build_tree(all_collections, None)

        items: dict[str, dict] = {}
        if self.hierarchy:
            node = self._find_by_path(tree, self.hierarchy.split(HIERARCHY_SEP))
            if node is None:
                raise ValueError(f"Zotero collection path not found: {self.hierarchy}")
            # Paths are relative to the synced collection, so it starts at root ("").
            self._merge(items, self._collect_items(node["key"], all_collections, ""))
        else:
            # Whole library: each top-level collection becomes a top-level directory.
            for node in tree:
                self._merge(items, self._collect_items(node["key"], all_collections, node["name"]))

        entries: list[ManifestEntry] = []
        for item_key, data in items.items():
            for path in data["paths"] or [""]:
                kb_path = path.replace(HIERARCHY_SEP, "/")
                for idx, att_key in enumerate(data["attachments"]):
                    filename = self._unique(kb_path, self._make_filename(data["title"], idx))
                    checksum, size = self._checksum(item_key, data["version"], att_key)
                    self._index[(kb_path, filename)] = att_key
                    entries.append(
                        ManifestEntry(filename=filename, path=kb_path, checksum=checksum, size=size)
                    )

        entries.sort(key=lambda e: e.display_path)
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        att_key = self._index.get((path, filename))
        if att_key is None:
            raise FileNotFoundError(f"Unknown Zotero file: {path}/{filename}")
        return self._get_text(att_key).encode("utf-8")

    # ── collection traversal ────────────────────────────────────

    def _build_tree(self, all_collections: list[dict], parent_key: str | None) -> list[dict]:
        """Build a hierarchical tree of collections under parent_key (None = top level)."""
        if parent_key is None:
            filtered = [c for c in all_collections if not c.get("data", {}).get("parentCollection")]
        else:
            filtered = [
                c for c in all_collections if c.get("data", {}).get("parentCollection") == parent_key
            ]
        return [
            {
                "key": c["key"],
                "name": c["data"]["name"],
                "children": self._build_tree(all_collections, c["key"]),
            }
            for c in filtered
        ]

    def _find_by_path(self, tree: list[dict], path_parts: list[str]) -> dict | None:
        """Navigate the tree following collection names; return the matching node."""
        if not path_parts:
            return None
        for node in tree:
            if node["name"] == path_parts[0]:
                if len(path_parts) == 1:
                    return node
                return self._find_by_path(node["children"], path_parts[1:])
        return None

    def _collect_items(
        self, collection_key: str, all_collections: list[dict], current_path: str
    ) -> dict[str, dict]:
        """Recursively gather items (with attachments) and the paths they appear under.

        Returns a dict: item_key -> {title, version, paths, attachments}. An item that
        lives in several subcollections records each path it appears under.
        """
        if self.excluded_paths and current_path:
            for ex in self.excluded_paths:
                if current_path == ex or current_path.startswith(f"{ex}{HIERARCHY_SEP}"):
                    return {}

        items: dict[str, dict] = {}
        for item in self._zot.everything(self._zot.collection_items(collection_key)):
            data = item.get("data", {})
            if data.get("itemType") == "attachment":
                continue  # standalone attachment, not a parent item
            item_key = item["key"]
            children = self._zot.everything(self._zot.children(item_key))
            attachments = [
                c["key"] for c in children if c.get("data", {}).get("itemType") == "attachment"
            ]
            if not attachments:
                continue
            if item_key not in items:
                items[item_key] = {
                    "title": data.get("title", "Untitled"),
                    "version": item.get("version", data.get("version", 0)),
                    "paths": [],
                    "attachments": attachments,
                }
            if current_path:
                if current_path not in items[item_key]["paths"]:
                    items[item_key]["paths"].append(current_path)
            elif not items[item_key]["paths"]:
                items[item_key]["paths"].append("")

        subcols = [
            c for c in all_collections if c.get("data", {}).get("parentCollection") == collection_key
        ]
        for sub in subcols:
            sub_name = sub["data"]["name"]
            new_path = f"{current_path}{HIERARCHY_SEP}{sub_name}" if current_path else sub_name
            self._merge(items, self._collect_items(sub["key"], all_collections, new_path))
        return items

    @staticmethod
    def _merge(dest: dict[str, dict], src: dict[str, dict]) -> None:
        """Merge src items into dest, unioning their path lists."""
        for key, value in src.items():
            if key not in dest:
                dest[key] = value
            else:
                for path in value["paths"]:
                    if path not in dest[key]["paths"]:
                        dest[key]["paths"].append(path)

    # ── naming, checksums, text extraction ──────────────────────

    @staticmethod
    def _make_filename(title: str, attachment_index: int, max_length: int = MAX_FILENAME) -> str:
        """Build a sanitized .txt filename for an item attachment."""
        name = re.sub(r"<[^>]+>", "", title)  # strip HTML tags Zotero embeds in titles
        name = name.replace("/", "_").replace("\\", "_").strip() or "Untitled"
        suffix = f"_{attachment_index + 1}" if attachment_index > 0 else ""
        ext = ".txt"
        budget = max_length - len(suffix) - len(ext)
        if len(name) > budget:
            name = name[: max(budget - 3, 10)] + "..."
        return f"{name}{suffix}{ext}"

    def _unique(self, path: str, filename: str) -> str:
        """Disambiguate filename collisions within the same KB directory."""
        if (path, filename) not in self._index:
            return filename
        stem, _, ext = filename.rpartition(".")
        i = 2
        while (path, f"{stem}__{i}.{ext}") in self._index:
            i += 1
        return f"{stem}__{i}.{ext}"

    def _checksum(self, item_key: str, version: int, attachment_key: str) -> tuple[str, int]:
        """Return (checksum, size) for an attachment per the configured checksum mode."""
        if self.checksum_mode == "content":
            try:
                data = self._get_text(attachment_key).encode("utf-8")
                return hashlib.sha256(data).hexdigest(), len(data)
            except Exception:
                # Text isn't retrievable (e.g. no file in Zotero storage). Never let one
                # bad attachment abort the whole manifest: fall back to the version
                # checksum so the entry is still created, and the failure surfaces (and is
                # reported) at upload time through the normal per-file error path.
                pass
        digest = hashlib.sha256(f"{item_key}:{version}:{attachment_key}".encode()).hexdigest()
        return digest, 0  # size unknown without downloading

    def _get_text(self, attachment_key: str) -> str:
        """Extract (and cache) the text of a Zotero attachment."""
        if attachment_key not in self._text_cache:
            self._text_cache[attachment_key] = self._extract_text(attachment_key)
        return self._text_cache[attachment_key]

    def _extract_text(self, attachment_key: str) -> str:
        """Get attachment text: Zotero's indexed fulltext first, else PyMuPDF on the PDF."""
        try:
            return self._zot.fulltext_item(attachment_key)["content"]
        except Exception:
            pass  # not indexed; fall back to downloading and extracting

        try:
            pdf_bytes = self._zot.file(attachment_key)
        except Exception as e:
            raise RuntimeError(
                f"Zotero has no downloadable file for attachment {attachment_key} ({e}). "
                "Its bytes aren't in Zotero storage, so neither the fulltext API nor the "
                "file endpoint can return content. Check that file syncing is enabled and "
                "your storage quota isn't exceeded, or whether this attachment is a web "
                "link or a WebDAV-only / linked file."
            ) from e
        try:
            import fitz  # pymupdf
        except ImportError as e:
            raise ImportError(
                "pymupdf is required for the Zotero connector. Install with: pip install oikb[zotero]"
            ) from e

        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as tmp:
            tmp.write(pdf_bytes)
            tmp_path = tmp.name
        try:
            doc = fitz.open(tmp_path)
            text = "".join(page.get_text() for page in doc)
            doc.close()
            return text
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def parse_zotero_source(source: str) -> dict[str, str | None]:
    hierarchy = source.removeprefix("zotero:")
    return {"hierarchy": hierarchy or None}
