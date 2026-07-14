"""ZOTERO_INCLUDE_NOTES / ZOTERO_INCLUDE_ANNOTATIONS behavior.

Covers the append feature end to end against a fake pyzotero client:
  - notes (child + standalone) and annotations get appended to the PDF .txt
  - a note-only item surfaces as its own file only when notes are enabled
  - both flags off reproduce the old PDF-only behavior
  - version-mode checksums react to note and annotation version bumps, since
    editing either bumps its own version, not the parent item's
"""

from __future__ import annotations

import copy

from oikb.connectors.zotero import ZoteroConnector

# A tiny fake library: one collection "Research" holding a journal article (with
# a PDF attachment, a child note, and a PDF annotation) and one standalone note.
COLLECTIONS = [{"key": "COLL1", "data": {"name": "Research"}}]

# Both top-level items are filed in COLL1 (collections set), so the unfiled sweep skips them.
ITEM_ARTICLE = {
    "key": "IT1",
    "version": 5,
    "data": {"itemType": "journalArticle", "title": "Paper A", "collections": ["COLL1"]},
}
ITEM_STANDALONE_NOTE = {
    "key": "IT2",
    "version": 3,
    "data": {"itemType": "note", "note": "<p>Standalone thought</p>", "collections": ["COLL1"]},
}
ATTACHMENT = {"key": "ATT1", "version": 6, "data": {"itemType": "attachment", "title": "Paper A.pdf"}}
CHILD_NOTE = {
    "key": "NOTE1",
    "version": 7,
    "data": {"itemType": "note", "note": "<p>Great <b>insight</b> here.</p>"},
}
ANNOTATION = {
    "key": "ANN1",
    "version": 9,
    "data": {
        "itemType": "annotation",
        "annotationText": "highlighted quote",
        "annotationComment": "my comment",
        "annotationSortIndex": "00001|00000|00000",
        "annotationPageLabel": "4",
    },
}


class _FakeZot:
    """Minimal pyzotero.Zotero stand-in driven by in-memory dicts."""

    def __init__(self, collections, items, children, fulltext, top=None):
        self._collections = collections
        self._items = items  # collection_key -> [item, ...]
        self._children = children  # parent_key -> [child, ...]
        self._fulltext = fulltext  # attachment_key -> str
        self._top = top or []  # top-level items across the library (for the unfiled sweep)

    def everything(self, x):  # pyzotero pages through generators; our data is already whole
        return x

    def collections(self):
        return self._collections

    def top(self):
        return self._top

    def collection_items(self, key):
        return self._items.get(key, [])

    def children(self, key):
        return self._children.get(key, [])

    def fulltext_item(self, key):
        if key in self._fulltext:
            return {"content": self._fulltext[key]}
        raise RuntimeError("not indexed")


def _make_conn(*, include_notes, include_annotations, checksum_mode="version", overrides=None):
    """Build a ZoteroConnector wired to the fake library, bypassing __init__/creds."""
    children = {
        "IT1": [copy.deepcopy(ATTACHMENT), copy.deepcopy(CHILD_NOTE)],
        "ATT1": [copy.deepcopy(ANNOTATION)],
    }
    items = {"COLL1": [copy.deepcopy(ITEM_ARTICLE), copy.deepcopy(ITEM_STANDALONE_NOTE)]}
    if overrides:
        overrides(children, items)

    conn = ZoteroConnector.__new__(ZoteroConnector)
    # top() returns the library's top-level items; here both are filed in COLL1.
    conn._zot = _FakeZot(COLLECTIONS, items, children, {"ATT1": "PDF body text"}, top=items["COLL1"])
    conn._index = {}
    conn._text_cache = {}
    conn._annotations_cache = {}
    conn.excluded_paths = set()
    conn.hierarchy = None
    conn.checksum_mode = checksum_mode
    conn.include_notes = include_notes
    conn.include_annotations = include_annotations
    conn.unfiled_dir = "_unfiled"
    return conn


def _read(conn, display_path):
    if not conn._index:  # read_file resolves against the index that build_manifest fills
        conn.build_manifest()
    path, _, filename = display_path.rpartition("/")
    return conn.read_file(path, filename).decode("utf-8")


def test_flags_off_is_pdf_only_and_skips_note_only_items():
    conn = _make_conn(include_notes=False, include_annotations=False)
    manifest = conn.build_manifest()

    paths = [e.display_path for e in manifest]
    # Only the article's PDF; the standalone note is not emitted.
    assert paths == ["Research/Paper A.txt"]
    assert _read(conn, "Research/Paper A.txt") == "PDF body text"


def test_notes_and_annotations_are_appended_to_pdf_text():
    conn = _make_conn(include_notes=True, include_annotations=True)
    manifest = conn.build_manifest()
    paths = {e.display_path for e in manifest}

    # The article PDF plus the standalone note as its own file.
    assert "Research/Paper A.txt" in paths
    assert "Research/Standalone thought.txt" in paths

    body = _read(conn, "Research/Paper A.txt")
    assert body == (
        "PDF body text\n\n"
        "=== NOTES ===\n"
        "Great insight here.\n\n"
        "=== ANNOTATIONS ===\n"
        "[p. 4] > highlighted quote\n"
        "  my comment"
    )


def test_note_only_item_surfaces_as_its_own_file():
    conn = _make_conn(include_notes=True, include_annotations=False)
    assert _read(conn, "Research/Standalone thought.txt") == "=== NOTES ===\nStandalone thought"


def test_annotations_disabled_leaves_them_out():
    conn = _make_conn(include_notes=True, include_annotations=False)
    body = _read(conn, "Research/Paper A.txt")
    assert "=== NOTES ===" in body
    assert "=== ANNOTATIONS ===" not in body
    assert "highlighted quote" not in body


def test_notes_disabled_leaves_them_out_but_keeps_annotations():
    conn = _make_conn(include_notes=False, include_annotations=True)
    body = _read(conn, "Research/Paper A.txt")
    assert "=== NOTES ===" not in body
    assert "=== ANNOTATIONS ===" in body


def _checksum_for(conn, display_path):
    entry = next(e for e in conn.build_manifest() if e.display_path == display_path)
    return entry.checksum


def test_version_checksum_reacts_to_note_edit():
    """Editing a note bumps only the note's version, so the checksum must still change."""
    base = _make_conn(include_notes=True, include_annotations=True)
    baseline = _checksum_for(base, "Research/Paper A.txt")

    def bump_note(children, items):
        children["IT1"][1]["version"] = 8  # was 7

    edited = _make_conn(include_notes=True, include_annotations=True, overrides=bump_note)
    assert _checksum_for(edited, "Research/Paper A.txt") != baseline


def test_version_checksum_reacts_to_annotation_edit():
    """Editing an annotation bumps only the annotation's version; checksum must change."""
    base = _make_conn(include_notes=True, include_annotations=True)
    baseline = _checksum_for(base, "Research/Paper A.txt")

    def bump_ann(children, items):
        children["ATT1"][0]["version"] = 10  # was 9

    edited = _make_conn(include_notes=True, include_annotations=True, overrides=bump_ann)
    assert _checksum_for(edited, "Research/Paper A.txt") != baseline


def test_content_checksum_covers_appended_sections():
    """In content mode the checksum hashes the assembled text, so appended notes shift it."""
    without = _make_conn(include_notes=False, include_annotations=False, checksum_mode="content")
    with_notes = _make_conn(include_notes=True, include_annotations=True, checksum_mode="content")
    assert _checksum_for(without, "Research/Paper A.txt") != _checksum_for(
        with_notes, "Research/Paper A.txt"
    )
