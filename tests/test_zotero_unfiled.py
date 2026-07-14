"""Unfiled 'My Library' items (in no collection) get swept into a virtual directory.

A whole-library sync (bare ``zotero:``) must surface items that sit directly in the library
with no collection, since the collection-tree walk can't see them. A named-hierarchy sync
must not. Covers directory naming/override, that filed items aren't duplicated, and that the
unfiled directory can be excluded.
"""

from __future__ import annotations

import copy

from oikb.connectors.zotero import ZoteroConnector

# One collection "Research" with a filed article, plus two items that sit directly in the
# library (empty `collections`): a loose PDF item and a loose standalone note.
COLLECTIONS = [{"key": "COLL1", "data": {"name": "Research"}}]

FILED_ARTICLE = {
    "key": "IT_FILED",
    "version": 5,
    "data": {"itemType": "journalArticle", "title": "Filed Paper", "collections": ["COLL1"]},
}
UNFILED_ARTICLE = {
    "key": "IT_LOOSE",
    "version": 9,
    "data": {"itemType": "journalArticle", "title": "Loose Paper", "collections": []},
}
UNFILED_NOTE = {
    "key": "IT_NOTE",
    "version": 4,
    "data": {"itemType": "note", "note": "<p>A loose thought</p>", "collections": []},
}

CHILDREN = {
    "IT_FILED": [{"key": "ATT_F", "version": 6, "data": {"itemType": "attachment"}}],
    "IT_LOOSE": [{"key": "ATT_L", "version": 8, "data": {"itemType": "attachment"}}],
}
FULLTEXT = {"ATT_F": "filed body", "ATT_L": "loose body"}


class _FakeZot:
    def __init__(self, *, top):
        self._top = top

    def everything(self, x):
        return x

    def collections(self):
        return COLLECTIONS

    def top(self):
        return self._top

    def collection_items(self, key):
        return [copy.deepcopy(FILED_ARTICLE)] if key == "COLL1" else []

    def children(self, key):
        return copy.deepcopy(CHILDREN.get(key, []))

    def fulltext_item(self, key):
        return {"content": FULLTEXT[key]}


def _make_conn(*, hierarchy, include_notes=False, exclude=None, unfiled_dir="_unfiled"):
    conn = ZoteroConnector.__new__(ZoteroConnector)
    conn._zot = _FakeZot(
        top=[copy.deepcopy(FILED_ARTICLE), copy.deepcopy(UNFILED_ARTICLE), copy.deepcopy(UNFILED_NOTE)]
    )
    conn._index = {}
    conn._text_cache = {}
    conn._annotations_cache = {}
    conn.excluded_paths = set(exclude or [])
    conn.hierarchy = hierarchy
    conn.checksum_mode = "version"
    conn.include_notes = include_notes
    conn.include_annotations = False
    conn.unfiled_dir = unfiled_dir
    return conn


def _paths(conn):
    return sorted(e.display_path for e in conn.build_manifest())


def test_bare_sync_sweeps_unfiled_items():
    conn = _make_conn(hierarchy=None, include_notes=True)
    paths = _paths(conn)

    # Filed item stays under its collection; loose items land under _unfiled/.
    assert "Research/Filed Paper.txt" in paths
    assert "_unfiled/Loose Paper.txt" in paths
    assert "_unfiled/A loose thought.txt" in paths  # note-only, enabled via include_notes

    # The loose PDF's content is its extracted body.
    assert conn.read_file("_unfiled", "Loose Paper.txt").decode() == "loose body"


def test_named_hierarchy_never_includes_unfiled():
    conn = _make_conn(hierarchy="Research", include_notes=True)
    paths = _paths(conn)

    assert paths == ["Filed Paper.txt"]  # synced root is the collection; no _unfiled sweep
    assert not any(p.startswith("_unfiled/") for p in paths)


def test_filed_items_are_not_duplicated_into_unfiled():
    """An item in a collection is top-level too, but its non-empty `collections` excludes it."""
    conn = _make_conn(hierarchy=None)
    paths = _paths(conn)
    assert paths.count("Research/Filed Paper.txt") == 1
    assert not any("Filed Paper" in p and p.startswith("_unfiled/") for p in paths)


def test_note_only_unfiled_item_needs_include_notes():
    conn = _make_conn(hierarchy=None, include_notes=False)
    paths = _paths(conn)
    # The loose PDF still appears, but the loose note does not without the flag.
    assert "_unfiled/Loose Paper.txt" in paths
    assert not any("loose thought" in p for p in paths)


def test_unfiled_dir_is_configurable():
    conn = _make_conn(hierarchy=None, unfiled_dir="Inbox")
    paths = _paths(conn)
    assert "Inbox/Loose Paper.txt" in paths
    assert not any(p.startswith("_unfiled/") for p in paths)


def test_unfiled_dir_can_be_excluded():
    conn = _make_conn(hierarchy=None, include_notes=True, exclude={"_unfiled"})
    paths = _paths(conn)
    assert paths == ["Research/Filed Paper.txt"]  # every unfiled entry dropped
