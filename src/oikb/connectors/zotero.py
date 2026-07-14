"""Zotero connector — sync PDF text from a Zotero library to a Knowledge Base.

Extracts text from the PDF attachments of items in a Zotero collection (and its
subcollections) and uploads them as .txt files. The collection hierarchy maps to
Knowledge Base directories.

This connector is READ-ONLY with respect to Zotero: it never modifies, deletes, or
adds anything to your Zotero library.

An attachment whose file isn't downloadable from Zotero (web link, linked file, or
bytes not in storage) is skipped with a warning rather than failing the sync: those
are Zotero-side data gaps, not oikb errors.

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
  ZOTERO_INCLUDE_NOTES        when truthy (1/true/yes/on), append the text of an item's
                       child notes to its extracted PDF .txt. An item that has notes but no
                       PDF still surfaces as its own notes-only .txt. Free: notes already
                       come back with the item's children, so no extra API calls.
  ZOTERO_INCLUDE_ANNOTATIONS  when truthy, append PDF highlights and comments
                       (annotationText / annotationComment) to each attachment's .txt. In
                       'version' checksum mode this costs one extra API call per attachment,
                       since annotations are children of the attachment, not of the item.
  ZOTERO_UNFILED_DIR   virtual directory name for library items that are in no collection
                       (default: "_unfiled"). Only used by a whole-library sync. Exclude it
                       entirely with ZOTERO_EXCLUDE=<that name>.

When both are enabled, an attachment's .txt is the PDF body, then a "=== NOTES ==="
section, then an "=== ANNOTATIONS ===" section. Change detection still works: 'content'
mode hashes the assembled text, and 'version' mode folds in the note and annotation
versions (editing a note or annotation bumps its own version, not the item's).

Source syntax: zotero:<hierarchy> where hierarchy uses %% as the separator, e.g.
  zotero:Research%%Machine Learning
An empty hierarchy (just "zotero:") syncs the whole library: every top-level collection
becomes a directory, and items sitting directly in "My Library" (in no collection) are
swept into the ZOTERO_UNFILED_DIR directory. A named hierarchy syncs only that subtree
and never includes unfiled items.
"""

from __future__ import annotations

import hashlib
import html
import os
import re
import tempfile

from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable

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
        include_notes: bool | None = None,
        include_annotations: bool | None = None,
        unfiled_dir: str | None = None,
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

        self.include_notes = (
            include_notes
            if include_notes is not None
            else self._env_flag(os.environ.get("ZOTERO_INCLUDE_NOTES", ""))
        )
        self.include_annotations = (
            include_annotations
            if include_annotations is not None
            else self._env_flag(os.environ.get("ZOTERO_INCLUDE_ANNOTATIONS", ""))
        )

        # Virtual directory for items that live directly in the library (no collection).
        raw_unfiled = (
            unfiled_dir if unfiled_dir is not None else os.environ.get("ZOTERO_UNFILED_DIR", "")
        )
        self.unfiled_dir = raw_unfiled.strip().strip("/") or "_unfiled"

        try:
            from pyzotero import zotero
        except ImportError as e:
            raise ImportError(
                "pyzotero is required for the Zotero connector. Install with: pip install oikb[zotero]"
            ) from e
        self._zot = zotero.Zotero(lib_id, lib_type, key)

        # (path, filename) -> record describing what read_file should assemble.
        # Record: {"attachment": key | None, "notes": [{"key", "version", "html"}]}.
        # attachment is None for a notes-only file (an item with notes but no PDF).
        self._index: dict[tuple[str, str], dict] = {}
        # attachment key -> extracted text, so read_file (and content-mode
        # checksumming) never extracts the same attachment twice.
        self._text_cache: dict[str, str] = {}
        # attachment key -> its annotation items, so we fetch each attachment's
        # annotations at most once (used by both checksum and read_file).
        self._annotations_cache: dict[str, list[dict]] = {}

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
            # Items that live directly in the library (in no collection) are invisible to
            # the collection walk, so sweep them into their own virtual directory.
            self._merge(items, self._collect_unfiled())

        entries: list[ManifestEntry] = []
        for item_key, data in items.items():
            notes = data["notes"] if self.include_notes else []
            for path in data["paths"] or [""]:
                kb_path = path.replace(HIERARCHY_SEP, "/")
                if data["attachments"]:
                    for idx, att_key in enumerate(data["attachments"]):
                        filename = self._unique(kb_path, self._make_filename(data["title"], idx))
                        checksum, size = self._checksum(item_key, data, att_key)
                        self._index[(kb_path, filename)] = {"attachment": att_key, "notes": notes}
                        entries.append(
                            ManifestEntry(
                                filename=filename, path=kb_path, checksum=checksum, size=size
                            )
                        )
                elif notes:
                    # An item with notes but no PDF: surface the notes on their own so
                    # enabling ZOTERO_INCLUDE_NOTES never silently drops note content.
                    filename = self._unique(kb_path, self._make_filename(data["title"], 0))
                    checksum, size = self._checksum(item_key, data, None)
                    self._index[(kb_path, filename)] = {"attachment": None, "notes": notes}
                    entries.append(
                        ManifestEntry(filename=filename, path=kb_path, checksum=checksum, size=size)
                    )

        entries.sort(key=lambda e: e.display_path)
        return entries

    def read_file(self, path: str, filename: str) -> bytes:
        record = self._index.get((path, filename))
        if record is None:
            raise FileNotFoundError(f"Unknown Zotero file: {path}/{filename}")
        return self._assemble(record["attachment"], record["notes"]).encode("utf-8")

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

        Returns a dict: item_key -> {title, version, paths, attachments, notes}. An item
        that lives in several subcollections records each path it appears under. `notes` is
        the list of the item's child notes (each {key, version, html}); it is populated
        regardless of self.include_notes so callers can decide, but is only emitted when the
        flag is on.
        """
        if self._is_excluded(current_path):
            return {}

        items: dict[str, dict] = {}
        for item in self._zot.everything(self._zot.collection_items(collection_key)):
            processed = self._process_item(item)
            if processed is None:
                continue
            item_key, record = processed
            if item_key not in items:
                items[item_key] = record
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

    def _collect_unfiled(self) -> dict[str, dict]:
        """Gather items that live directly in the library, in no collection.

        The collection walk can't see these, and the Zotero API has no "unfiled" query, so
        fetch every top-level item and keep the ones whose `collections` list is empty. They
        are all routed into the single virtual self.unfiled_dir directory.
        """
        if self._is_excluded(self.unfiled_dir):
            return {}
        items: dict[str, dict] = {}
        for item in self._zot.everything(self._zot.top()):
            if item.get("data", {}).get("collections"):
                continue  # filed in at least one collection; the tree walk handles it
            processed = self._process_item(item)
            if processed is None:
                continue
            item_key, record = processed
            record["paths"] = [self.unfiled_dir]
            items[item_key] = record
        return items

    def _is_excluded(self, path: str) -> bool:
        """True if `path` equals or sits under any ZOTERO_EXCLUDE entry."""
        if not path:
            return False
        return any(
            path == ex or path.startswith(f"{ex}{HIERARCHY_SEP}") for ex in self.excluded_paths
        )

    def _process_item(self, item: dict) -> tuple[str, dict] | None:
        """Reduce one Zotero item to a manifest record, or None if it has nothing to emit.

        Shared by the collection walk and the unfiled sweep. Returns (item_key, record)
        with record = {title, version, paths (empty; the caller fills it), attachments,
        notes}. Returns None for standalone attachments and for items that have neither a
        PDF nor (when ZOTERO_INCLUDE_NOTES is on) any notes.
        """
        data = item.get("data", {})
        item_type = data.get("itemType")
        if item_type == "attachment":
            return None  # standalone attachment, not a parent item
        item_key = item["key"]
        if item_type == "note":
            # A standalone note: it is its own content and has no children.
            attachments: list[str] = []
            notes = [self._note_record(item)]
            title = self._note_title(notes[0]["html"])
        else:
            children = self._zot.everything(self._zot.children(item_key))
            attachments = [
                c["key"] for c in children if c.get("data", {}).get("itemType") == "attachment"
            ]
            notes = [
                self._note_record(c)
                for c in children
                if c.get("data", {}).get("itemType") == "note"
            ]
            title = data.get("title", "Untitled")
        # Skip only when there is genuinely nothing to emit: no PDF, and either notes are
        # disabled or the item has none. With ZOTERO_INCLUDE_NOTES on, note-only items still
        # surface (build_manifest gives them a notes-only .txt).
        if not attachments and not (self.include_notes and notes):
            return None
        return item_key, {
            "title": title,
            "version": item.get("version", data.get("version", 0)),
            "paths": [],
            "attachments": attachments,
            "notes": notes,
        }

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

    def _checksum(self, item_key: str, item: dict, attachment_key: str | None) -> tuple[str, int]:
        """Return (checksum, size) for one KB file per the configured checksum mode.

        A KB file is a PDF attachment plus, when enabled, the item's notes and the
        attachment's annotations; the checksum must cover all of them. `attachment_key` is
        None for a notes-only item.
        """
        notes = item["notes"] if self.include_notes else []
        if self.checksum_mode == "content":
            try:
                data = self._assemble(attachment_key, notes).encode("utf-8")
                return hashlib.sha256(data).hexdigest(), len(data)
            except Exception:
                # Text isn't retrievable (e.g. no file in Zotero storage). Never let one
                # bad attachment abort the whole manifest: fall back to the version
                # checksum so the entry is still created, and the failure surfaces (and is
                # reported) at upload time through the normal per-file error path.
                pass
        # Version mode. Editing a note or annotation bumps its own version, not the parent
        # item's, so fold those versions in or such edits would go undetected.
        parts = [item_key, str(item["version"]), attachment_key or ""]
        if self.include_notes:
            parts.append("n:" + ",".join(f"{n['key']}={n['version']}" for n in notes))
        if self.include_annotations and attachment_key is not None:
            try:
                anns = self._get_annotations(attachment_key)
                parts.append("a:" + ",".join(f"{a['key']}={self._version_of(a)}" for a in anns))
            except Exception:
                pass  # can't list annotations now; the failure resurfaces at read time
        digest = hashlib.sha256(":".join(parts).encode()).hexdigest()
        return digest, 0  # size unknown without downloading

    # ── notes & annotations ─────────────────────────────────────

    @staticmethod
    def _env_flag(raw: str) -> bool:
        """Interpret an env var as a boolean (1/true/yes/on, case-insensitive)."""
        return raw.strip().lower() in ("1", "true", "yes", "on")

    @staticmethod
    def _version_of(item: dict) -> int:
        """A Zotero item's version, whether it sits at the top level or under 'data'."""
        return item.get("version", item.get("data", {}).get("version", 0))

    @classmethod
    def _note_record(cls, item: dict) -> dict:
        """Reduce a note item to what we need: its key, version, and raw HTML body."""
        return {
            "key": item["key"],
            "version": cls._version_of(item),
            "html": item.get("data", {}).get("note", ""),
        }

    @classmethod
    def _note_title(cls, note_html: str, max_length: int = 60) -> str:
        """Derive a filename title for a standalone note from its first line of text."""
        text = cls._html_to_text(note_html)
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first:
            return "Untitled"
        return first[:max_length].strip()

    @staticmethod
    def _html_to_text(note_html: str) -> str:
        """Flatten a Zotero note's HTML into plain text (no external dependency)."""
        if not note_html:
            return ""
        s = re.sub(r"(?i)<br\s*/?>", "\n", note_html)
        s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|blockquote)>", "\n", s)
        s = re.sub(r"(?i)<li[^>]*>", "- ", s)
        s = re.sub(r"<[^>]+>", "", s)  # drop any remaining tags
        s = html.unescape(s)
        s = re.sub(r"[ \t]+\n", "\n", s)
        s = re.sub(r"\n{3,}", "\n\n", s)
        return s.strip()

    def _assemble(self, attachment_key: str | None, notes: list[dict]) -> str:
        """Build the .txt body: PDF text, then a notes section, then an annotations section.

        `notes` is empty when ZOTERO_INCLUDE_NOTES is off; annotations are only fetched when
        ZOTERO_INCLUDE_ANNOTATIONS is on. May raise SourceFileUnavailable if the PDF's bytes
        can't be fetched (handled upstream as a non-fatal skip).
        """
        parts: list[str] = []
        if attachment_key is not None:
            parts.append(self._get_text(attachment_key))
        if notes:
            body = self._render_notes(notes)
            if body:
                parts.append("=== NOTES ===\n" + body)
        if self.include_annotations and attachment_key is not None:
            body = self._render_annotations(attachment_key)
            if body:
                parts.append("=== ANNOTATIONS ===\n" + body)
        return "\n\n".join(p for p in parts if p)

    def _render_notes(self, notes: list[dict]) -> str:
        """Render note records to plain text, one block per note."""
        blocks = [self._html_to_text(n["html"]) for n in notes]
        return "\n\n".join(b for b in blocks if b)

    def _get_annotations(self, attachment_key: str) -> list[dict]:
        """Fetch (and cache) an attachment's annotation items, in reading order."""
        if attachment_key not in self._annotations_cache:
            children = self._zot.everything(self._zot.children(attachment_key))
            anns = [
                c for c in children if c.get("data", {}).get("itemType") == "annotation"
            ]
            anns.sort(key=lambda a: a.get("data", {}).get("annotationSortIndex", ""))
            self._annotations_cache[attachment_key] = anns
        return self._annotations_cache[attachment_key]

    def _render_annotations(self, attachment_key: str) -> str:
        """Render an attachment's highlights/comments to plain text, in reading order."""
        blocks: list[str] = []
        for ann in self._get_annotations(attachment_key):
            data = ann.get("data", {})
            quote = (data.get("annotationText") or "").strip()
            comment = (data.get("annotationComment") or "").strip()
            page = (data.get("annotationPageLabel") or "").strip()
            prefix = f"[p. {page}] " if page else ""
            lines: list[str] = []
            if quote:
                lines.append(f"{prefix}> {quote}")
            if comment:
                lines.append(f"{prefix}{comment}" if not quote else f"  {comment}")
            if lines:
                blocks.append("\n".join(lines))
        return "\n\n".join(blocks)

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
            # The attachment exists in Zotero's metadata but its bytes aren't
            # retrievable. This is a Zotero-side data gap, not an oikb failure, so
            # raise SourceFileUnavailable: the sync skips this one file with a
            # warning instead of aborting / failing the whole run.
            raise SourceFileUnavailable(
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
