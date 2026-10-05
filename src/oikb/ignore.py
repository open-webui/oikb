"""Gitignore-style pattern matching for .oikbignore files."""

from __future__ import annotations

import fnmatch
from collections.abc import Iterator
from pathlib import Path


def load_ignore_patterns(root: Path) -> list[str]:
    """Load patterns from .oikbignore file in the given directory.

    Supports gitignore-style patterns:
      - Blank lines and lines starting with # are ignored
      - Standard glob patterns (*, ?, [])
      - **/ matches any number of directories, including none
      - Leading / anchors to the root
      - Trailing / matches directories only
      - ! negates a pattern (not yet supported)
    """
    ignore_file = root / ".oikbignore"
    if not ignore_file.exists():
        return []

    patterns: list[str] = []
    with open(ignore_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            patterns.append(line)

    return patterns


def should_ignore(
    relative_path: str,
    name: str,
    is_dir: bool,
    patterns: list[str],
) -> bool:
    """Check if a file or directory should be ignored based on .oikbignore patterns.

    Args:
        relative_path: Path relative to root (e.g. "docs/api/readme.md").
        name:          Basename (e.g. "readme.md").
        is_dir:        Whether the entry is a directory.
        patterns:      List of gitignore-style patterns.
    """
    for pattern in patterns:
        # Directory-only patterns (trailing /).
        dir_only = pattern.endswith("/")
        if dir_only:
            if not is_dir:
                continue
            pattern = pattern.rstrip("/")

        # Match against both the basename and the full relative path.
        if glob_match(name, pattern):
            return True
        if glob_match(relative_path, pattern):
            return True

    return False


def glob_match(path: str, pattern: str) -> bool:
    """Match like fnmatch, except that each **/ may also stand for no directory.

    fnmatch's * already crosses /, but the / in **/ must still be matched, so
    plain fnmatch skips "README.md" for "**/*.md" and "docs/index.md" for
    "docs/**/*.md".
    """
    return any(fnmatch.fnmatch(path, p) for p in _double_star_variants(pattern))


def _double_star_variants(pattern: str) -> Iterator[str]:
    """Yield pattern with each leading or /-preceded **/ kept or dropped."""
    head, sep, tail = pattern.partition("**/")
    if not sep:
        yield pattern
        return
    for rest in _double_star_variants(tail):
        yield head + sep + rest
        # As in gitignore, "a**/b" is an ordinary * and never matches "ab".
        if not head or head.endswith("/"):
            yield head + rest
