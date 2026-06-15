"""Helpers for syncing one or more .oikb.yaml entries into a Knowledge Base."""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable

from oikb.connectors import BaseConnector, ManifestEntry
from oikb.connectors.composite import CompositeConnector
from oikb.sync import SyncResult, build_manifest_filter, parse_size, run_sync


def group_entries_by_kb(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group yaml entries that share the same kb-id."""
    groups: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
    for entry in entries:
        groups.setdefault(entry["kb-id"], []).append(entry)
    return list(groups.values())


def manifest_filter_for_entry(
    entry: dict[str, Any],
    max_file_size: str | None = None,
) -> Callable | None:
    entry_filter = entry.get("filter", {})
    include = entry_filter.get("include")
    exclude = entry_filter.get("exclude")
    max_size = entry_filter.get("max-size") or max_file_size
    if not include and not exclude and not max_size:
        return None
    return build_manifest_filter(
        include=include,
        exclude=exclude,
        max_size=parse_size(max_size),
    )


def build_connector_for_entries(
    entries: list[dict[str, Any]],
    resolve_connector: Callable[..., BaseConnector],
    max_file_size: str | None = None,
) -> BaseConnector:
    """Build a single connector for one yaml entry or a merged composite."""
    if len(entries) == 1:
        entry = entries[0]
        return resolve_connector(
            entry["source"],
            entry.get("branch"),
            entry.get("path"),
        )

    parts: list[tuple[BaseConnector, list[ManifestEntry]]] = []
    for entry in entries:
        connector = resolve_connector(
            entry["source"],
            entry.get("branch"),
            entry.get("path"),
        )
        manifest = connector.build_manifest()
        manifest_filter = manifest_filter_for_entry(entry, max_file_size)
        if manifest_filter:
            manifest = manifest_filter(manifest)
        parts.append((connector, manifest))

    return CompositeConnector(parts)


def sources_label(entries: list[dict[str, Any]]) -> str:
    if len(entries) == 1:
        return entries[0].get("source", "?")
    return "+".join(entry.get("source", "?") for entry in entries)


def run_entries_sync(
    client: Any,
    entries: list[dict[str, Any]],
    *,
    resolve_connector: Callable[..., BaseConnector],
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    concurrency: int = 1,
    max_file_size: str | None = None,
) -> SyncResult:
    """Sync one kb-id group (single source or merged composite)."""
    if len(entries) == 1:
        entry = entries[0]
        connector = build_connector_for_entries(entries, resolve_connector, max_file_size)
        return run_sync(
            client=client,
            connector=connector,
            kb_id=entry["kb-id"],
            dry_run=dry_run,
            verbose=verbose,
            quiet=quiet,
            manifest_filter=manifest_filter_for_entry(entry, max_file_size),
            concurrency=entry.get("concurrency", concurrency),
        )

    connector = build_connector_for_entries(entries, resolve_connector, max_file_size)
    return run_sync(
        client=client,
        connector=connector,
        kb_id=entries[0]["kb-id"],
        dry_run=dry_run,
        verbose=verbose,
        quiet=quiet,
        concurrency=max(entry.get("concurrency", concurrency) for entry in entries),
    )
