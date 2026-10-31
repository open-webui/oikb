from __future__ import annotations

import pytest

from oikb.connectors import ManifestEntry
from oikb.ignore import glob_match, should_ignore
from oikb.sync import build_manifest_filter


def _entry(display_path: str) -> ManifestEntry:
    path, _, filename = display_path.rpartition("/")
    return ManifestEntry(filename, path, "checksum", 1)


def test_filter_double_star_matches_top_level_files() -> None:
    manifest = [
        _entry(p)
        for p in (
            "Note.md",
            "Clippings/Gazpacho.md",
            "docs/index.md",
            "docs/guide/setup.md",
            "scratch.tmp",
            "docs/old.tmp",
        )
    ]

    def kept(**patterns: list[str]) -> list[str]:
        return [e.display_path for e in build_manifest_filter(**patterns)(manifest)]

    markdown = ["Note.md", "Clippings/Gazpacho.md", "docs/index.md", "docs/guide/setup.md"]
    assert kept(include=["**/*.md"]) == markdown
    assert kept(include=["docs/**/*.md"]) == ["docs/index.md", "docs/guide/setup.md"]
    assert kept(exclude=["**/*.tmp"]) == markdown


def test_oikbignore_double_star_matches_at_the_root() -> None:
    patterns = ["**/*.tmp", "**/archive/"]

    assert should_ignore("scratch.tmp", "scratch.tmp", False, patterns)
    assert should_ignore("archive", "archive", True, patterns)
    assert should_ignore("docs/archive", "archive", True, patterns)
    assert not should_ignore("archives", "archives", True, patterns)


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("a/x/b/c.md", "a/**/b/**/c.md", True),
        ("a/b/c.md", "a/**/b/**/c.md", True),
        ("mydocs/index.md", "docs/**/*.md", False),
        ("docs.md", "docs/**/*.md", False),
        ("notes.mdx", "**/*.md", False),
        # ** that does not start a path segment is a plain *, as in gitignore.
        ("ab", "a**/b", False),
        # fnmatch's * still crosses /, which the Confluence tree filters rely on.
        ("FAQ/Benefits.txt", "FAQ*", True),
    ],
)
def test_glob_match(path: str, pattern: str, expected: bool) -> None:
    assert glob_match(path, pattern) is expected
