"""Sync orchestrator — diff → cleanup → mkdir → upload."""

from __future__ import annotations

import fnmatch
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Callable

import click
import httpx
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

from oikb.client import OikbClient
from oikb.connectors import BaseConnector, ManifestEntry, SourceFileUnavailable
from oikb.history import SyncHistory

# Stderr console for progress output (keeps stdout clean for piping).
_console = Console(stderr=True)

# HTTP statuses that mean "this file will never be accepted as-is"
# (rejected content, conflicts, too large, unsupported type). Auth failures
# (401/403) are deliberately excluded — they are config problems that must
# keep surfacing as errors on every run, not get silently skipped.
_PERMANENT_UPLOAD_STATUSES = frozenset({400, 409, 413, 415, 422})


@dataclass
class SyncResult:
    """Summary of a completed sync operation."""

    added: int = 0
    modified: int = 0
    deleted: int = 0
    unmodified: int = 0
    dirs_created: int = 0
    dirs_removed: int = 0
    skipped_failed: int = 0
    skipped_pending: int = 0
    errors: list[str] | None = None
    warnings: list[str] | None = None

    @property
    def total_changes(self) -> int:
        return self.added + self.modified + self.deleted

    def summary(self) -> str:
        parts = []
        if self.added:
            parts.append(f"{self.added} added")
        if self.modified:
            parts.append(f"{self.modified} modified")
        if self.deleted:
            parts.append(f"{self.deleted} deleted")
        if self.unmodified:
            parts.append(f"{self.unmodified} unchanged")
        if self.dirs_created:
            parts.append(f"{self.dirs_created} dirs created")
        if self.dirs_removed:
            parts.append(f"{self.dirs_removed} dirs removed")
        if self.skipped_failed:
            parts.append(f"{self.skipped_failed} skipped (known failures)")
        if self.skipped_pending:
            parts.append(f"{self.skipped_pending} skipped (still processing)")
        return ", ".join(parts) if parts else "nothing to do"


class SyncCancelled(Exception):
    """Raised when a running sync is asked to stop."""


def parse_size(value: str | int | None) -> int | None:
    """Parse a human-readable size string to bytes.

    Examples: '50mb' → 52428800, '1gb' → 1073741824, '500kb' → 512000.
    Returns None if value is None or empty.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)

    value = value.strip().lower()
    multipliers = {"b": 1, "kb": 1024, "mb": 1024 ** 2, "gb": 1024 ** 3}

    for suffix, mult in sorted(multipliers.items(), key=lambda x: -len(x[0])):
        if value.endswith(suffix):
            return int(float(value[: -len(suffix)].strip()) * mult)

    return int(value)


def build_manifest_filter(
    include: list[str] | None = None,
    exclude: list[str] | None = None,
    max_size: int | None = None,
) -> Callable[[list[ManifestEntry]], list[ManifestEntry]] | None:
    """Build a filter function from glob include/exclude patterns and size limit.

    Returns None if no filtering is needed.
    """
    if not include and not exclude and max_size is None:
        return None

    def _filter(entries: list[ManifestEntry]) -> list[ManifestEntry]:
        result = []
        for entry in entries:
            path = entry.display_path
            if include and not any(fnmatch.fnmatch(path, p) for p in include):
                continue
            if exclude and any(fnmatch.fnmatch(path, p) for p in exclude):
                continue
            if max_size is not None and entry.size > max_size:
                click.echo(
                    click.style(
                        f"  ⚠ Skipping {path} ({_fmt_size(entry.size)}) "
                        f"— exceeds max-size ({_fmt_size(max_size)})",
                        fg="yellow",
                    ),
                    err=True,
                )
                continue
            result.append(entry)
        return result

    return _filter


def _fmt_size(n: int) -> str:
    """Format bytes as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_sync(
    client: OikbClient,
    connector: BaseConnector,
    kb_id: str,
    dry_run: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    manifest_filter: Callable[[list[ManifestEntry]], list[ManifestEntry]] | None = None,
    concurrency: int = 1,
    cancel_requested: Callable[[], bool] | None = None,
    history: SyncHistory | None = None,
) -> SyncResult:
    """Execute a full incremental sync.

    Steps:
      1. Build manifest from connector
      2. Apply optional manifest filter
      3. POST manifest to /sync/diff
      4. Cleanup stale files (delete before upload)
      5. Create missing directories
      6. Upload added + modified files

    Files whose upload was previously rejected with a permanent (4xx) status
    are skipped until their checksum changes (see SyncHistory.failed_file).
    """
    result = SyncResult()
    result.errors = []
    result.warnings = []

    owns_history = history is None
    if history is None:
        history = SyncHistory()

    try:
        return _run_sync_inner(
            client, connector, kb_id, dry_run, verbose, quiet,
            manifest_filter, concurrency, result, cancel_requested, history,
        )
    finally:
        connector.close()
        if owns_history and history is not None:
            history.close()


def _run_sync_inner(
    client: OikbClient,
    connector: BaseConnector,
    kb_id: str,
    dry_run: bool,
    verbose: bool,
    quiet: bool,
    manifest_filter: Callable[[list[ManifestEntry]], list[ManifestEntry]] | None,
    concurrency: int,
    result: SyncResult,
    cancel_requested: Callable[[], bool] | None,
    history: SyncHistory | None,
) -> SyncResult:
    """Inner sync logic, separated for clean connector cleanup."""
    show_progress = not quiet and not dry_run

    def check_stop() -> None:
        if cancel_requested and cancel_requested():
            raise SyncCancelled("sync cancelled")

    # ── 1. Build manifest ──────────────────────────────────────
    check_stop()
    if show_progress:
        with _console.status("[bold blue]Scanning source..."):
            manifest = connector.build_manifest()
        _console.print(f"  [dim]{len(manifest)} files found[/dim]")
    else:
        if verbose:
            click.echo("Scanning source...", err=True)
        manifest = connector.build_manifest()
        if verbose:
            click.echo(f"  {len(manifest)} files found", err=True)

    # ── 2. Apply filter ────────────────────────────────────────
    check_stop()
    if manifest_filter:
        manifest = manifest_filter(manifest)
        if show_progress:
            _console.print(f"  [dim]{len(manifest)} files after filtering[/dim]")
        elif verbose:
            click.echo(f"  {len(manifest)} files after filtering", err=True)

    if not manifest:
        if not quiet:
            click.echo("Source is empty — nothing to sync.", err=True)
        return result

    # ── 3. Compute diff ────────────────────────────────────────
    check_stop()
    if show_progress:
        with _console.status("[bold blue]Computing diff..."):
            diff = client.sync_diff(kb_id, [e.to_dict() for e in manifest])
    else:
        if verbose:
            click.echo("Computing diff...", err=True)
        diff = client.sync_diff(kb_id, [e.to_dict() for e in manifest])

    added: list[dict[str, Any]] = diff.get("added", [])
    modified: list[dict[str, Any]] = diff.get("modified", [])
    deleted: list[dict[str, Any]] = diff.get("deleted", [])
    unmodified_count: int = diff.get("unmodified_count", 0)
    mkdir: list[str] = diff.get("mkdir", [])
    rmdir: list[str] = diff.get("rmdir", [])
    directory_map: dict[str, str] = diff.get("directory_map", {})

    result.unmodified = unmodified_count

    # ── 3b. Skip files with known permanent failures ──────────
    manifest_by_key = {(e.path, e.filename): e for e in manifest}

    known_failures = history.failed_checksums(kb_id) if history else {}
    if known_failures:
        def _is_known_failure(entry: dict[str, Any]) -> bool:
            key = (entry.get("path", ""), entry["filename"])
            manifest_entry = manifest_by_key.get(key)
            return (
                manifest_entry is not None
                and known_failures.get(key) == manifest_entry.checksum
            )

        before = len(added) + len(modified)
        added = [e for e in added if not _is_known_failure(e)]
        modified = [e for e in modified if not _is_known_failure(e)]
        result.skipped_failed = before - len(added) - len(modified)
        if result.skipped_failed and not quiet:
            click.echo(
                click.style(
                    f"  Skipping {result.skipped_failed} file(s) previously rejected "
                    f"by the server (manage via 'oikb failures')",
                    fg="yellow",
                ),
                err=True,
            )

    # ── 3c. Reconcile tracked uploads instead of re-uploading ──
    # The server ingests uploads asynchronously and never reports ingestion
    # failures to the upload call (HTTP 200, then the file silently lands in
    # status 'failed' or is never linked to the KB). Such files reappear as
    # "added" in every diff. Instead of re-uploading them (which would create
    # yet another server-side copy each cycle), check their stored status.
    if history and not dry_run:
        tracked = history.tracked_uploads(kb_id)
        if tracked:
            reported = {(e.get("path", ""), e["filename"]) for e in (*added, *modified)}

            def _drop(key: tuple[str, str]) -> None:
                nonlocal added, modified
                added = [e for e in added if (e.get("path", ""), e["filename"]) != key]
                modified = [e for e in modified if (e.get("path", ""), e["filename"]) != key]

            for key, rec in tracked.items():
                if key not in reported:
                    # No longer reported as changed → linked fine (or removed
                    # from the source and handled by cleanup). Stop tracking.
                    history.untrack_upload(kb_id, *key)
                    continue

                manifest_entry = manifest_by_key.get(key)
                if manifest_entry is None or manifest_entry.checksum != rec["checksum"]:
                    continue  # source changed → fall through to normal re-upload

                try:
                    status = client.get_file_status(rec["file_id"])
                except Exception:
                    status = "unknown"

                if status in ("pending", "processing", "unknown"):
                    # Still ingesting (or status unreadable) → skip this run,
                    # check again on the next one. No re-upload.
                    _drop(key)
                    result.skipped_pending += 1
                elif status == "failed":
                    history.record_failure(
                        kb_id, key[0], key[1], rec["checksum"],
                        "server-side ingestion failed",
                    )
                    history.untrack_upload(kb_id, *key)
                    _drop(key)
                    result.skipped_failed += 1
                elif status == "completed":
                    # Processed fine but never linked (e.g. duplicate content
                    # rejection, or manual removal from the KB). Try re-linking
                    # the existing file instead of uploading another copy.
                    try:
                        client.add_file_to_kb(
                            kb_id, rec["file_id"],
                            directory_id=directory_map.get(key[0]) or None,
                        )
                        history.untrack_upload(kb_id, *key)
                        _drop(key)
                    except httpx.HTTPStatusError as e:
                        if e.response.status_code in _PERMANENT_UPLOAD_STATUSES:
                            history.record_failure(
                                kb_id, key[0], key[1], rec["checksum"], str(e)
                            )
                            history.untrack_upload(kb_id, *key)
                            _drop(key)
                            result.skipped_failed += 1
                        else:
                            raise
                else:
                    # File is gone server-side (None) → stop tracking and let
                    # the normal upload path below re-upload it.
                    history.untrack_upload(kb_id, *key)

    if show_progress:
        parts = []
        if added:
            parts.append(f"[green]+{len(added)}[/green]")
        if modified:
            parts.append(f"[yellow]~{len(modified)}[/yellow]")
        if deleted:
            parts.append(f"[red]-{len(deleted)}[/red]")
        if unmodified_count:
            parts.append(f"[dim]{unmodified_count} unchanged[/dim]")
        _console.print(f"  Diff: {', '.join(parts)}" if parts else "  [dim]Nothing to do[/dim]")

    # ── Dry run: just print what would happen ──────────────────
    if dry_run:
        result.added = len(added)
        result.modified = len(modified)
        result.deleted = len(deleted)
        result.dirs_created = len(mkdir)
        result.dirs_removed = len(rmdir)

        if added:
            click.echo(click.style("+ Added:", fg="green"))
            for f in added:
                _echo_file_entry(f, "+", "green")

        if modified:
            click.echo(click.style("~ Modified:", fg="yellow"))
            for f in modified:
                _echo_file_entry(f, "~", "yellow")

        if deleted:
            click.echo(click.style("- Deleted:", fg="red"))
            for f in deleted:
                _echo_file_entry(f, "-", "red")

        if mkdir:
            click.echo(click.style("📁 Dirs to create:", fg="cyan"))
            for d in mkdir:
                click.echo(f"  + {d}")

        if rmdir:
            click.echo(click.style("📁 Dirs to remove:", fg="cyan"))
            for d in rmdir:
                click.echo(f"  - {d}")

        return result

    # Nothing to do?
    if not added and not modified and not deleted and not mkdir and not rmdir:
        return result

    # ── 4. Cleanup stale files ─────────────────────────────────
    stale_file_ids = [
        *[d["file_id"] for d in deleted],
        *[m["stale_file_id"] for m in modified],
    ]

    if stale_file_ids or rmdir:
        check_stop()
        if show_progress:
            with _console.status(f"[bold blue]Cleaning up {len(stale_file_ids)} stale files..."):
                client.sync_cleanup(kb_id, stale_file_ids, rmdir if rmdir else None)
        else:
            if verbose:
                click.echo(
                    f"Cleaning up {len(stale_file_ids)} files, {len(rmdir)} dirs...",
                    err=True,
                )
            client.sync_cleanup(kb_id, stale_file_ids, rmdir if rmdir else None)
        result.deleted = len(deleted)
        result.dirs_removed = len(rmdir)

    # ── 5. Create missing directories ──────────────────────────
    for dir_path in mkdir:
        check_stop()
        segments = dir_path.split("/")
        name = segments[-1]
        parent_path = "/".join(segments[:-1])
        parent_id = directory_map.get(parent_path)

        if verbose:
            click.echo(f"  mkdir {dir_path}", err=True)

        resp = client.create_directory(kb_id, name, parent_id)
        directory_map[dir_path] = resp.get("id", "")
        result.dirs_created += 1

    # ── 6. Upload files ────────────────────────────────────────

    files_to_upload = [
        *[(a, "added") for a in added],
        *[(m, "modified") for m in modified],
    ]

    if not files_to_upload:
        return result

    def _upload_one(
        i: int, entry: dict, change_type: str, progress: Progress | None, task_id: Any,
    ) -> tuple[str, str | None]:
        """Upload a single file with retry."""
        filename = entry["filename"]
        path = entry.get("path", "")
        display = f"{path}/{filename}" if path else filename

        if verbose and not progress:
            click.echo(f"  [{i}/{len(files_to_upload)}] {display}", err=True)

        check_stop()
        manifest_entry = manifest_by_key.get((path, filename))
        if not manifest_entry:
            return ("error", f"File not in manifest: {display}")

        last_err: Exception | None = None
        for attempt in range(3):
            check_stop()
            try:
                content = connector.read_file(path, filename)
                check_stop()
                directory_id = directory_map.get(path) if path else None
                resp = client.upload_file(
                    file_content=content,
                    filename=filename,
                    kb_id=kb_id,
                    file_hash=manifest_entry.checksum,
                    directory_id=directory_id,
                )
                if progress is not None:
                    progress.update(task_id, advance=1, description=f"[cyan]{display}[/cyan]")
                if history is not None:
                    history.clear_failure(kb_id, path, filename)
                    file_id = resp.get("id") if isinstance(resp, dict) else None
                    if file_id:
                        history.record_upload(
                            kb_id, path, filename, manifest_entry.checksum, file_id
                        )
                return (change_type, None)
            except SourceFileUnavailable as e:
                message = f"{display}: {e}"
                if progress is not None:
                    progress.update(task_id, advance=1, description=f"[yellow]⚠ {display}[/yellow]")
                else:
                    click.echo(click.style(f"  ⚠ {message}", fg="yellow"), err=True)
                return ("warning", message)
            except httpx.HTTPStatusError as e:
                if e.response.status_code >= 500 and attempt < 2:
                    time.sleep(2 ** attempt)
                    check_stop()
                    last_err = e
                    continue
                last_err = e
                break
            except SyncCancelled:
                raise
            except Exception as e:
                last_err = e
                break

        if progress is not None:
            progress.update(task_id, advance=1, description=f"[red]✗ {display}[/red]")
        else:
            click.echo(click.style(f"  ✗ {display}: {last_err}", fg="red"), err=True)
        if (
            history is not None
            and isinstance(last_err, httpx.HTTPStatusError)
            and last_err.response.status_code in _PERMANENT_UPLOAD_STATUSES
        ):
            history.record_failure(
                kb_id, path, filename, manifest_entry.checksum, str(last_err)
            )
        return ("error", f"{display}: {last_err}")

    def _tally(outcome: tuple[str, str | None]) -> None:
        """Update result counters from an upload outcome."""
        kind, message = outcome
        if kind == "added":
            result.added += 1
        elif kind == "modified":
            result.modified += 1
        elif kind == "warning" and message is not None:
            result.warnings.append(message)
        elif message is not None:
            result.errors.append(message)

    if show_progress:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("[bold blue]Uploading"),
            BarColumn(bar_width=30),
            MofNCompleteColumn(),
            TextColumn("•"),
            TextColumn("{task.description}"),
            TextColumn("•"),
            TimeElapsedColumn(),
            console=_console,
            transient=True,
        )
        with progress:
            task_id = progress.add_task("", total=len(files_to_upload))

            if concurrency > 1 and len(files_to_upload) > 1:
                with ThreadPoolExecutor(max_workers=concurrency) as pool:
                    futures = {
                        pool.submit(_upload_one, i, entry, ct, progress, task_id): (entry, ct)
                        for i, (entry, ct) in enumerate(files_to_upload, 1)
                    }
                    for future in as_completed(futures):
                        _tally(future.result())
            else:
                for i, (entry, change_type) in enumerate(files_to_upload, 1):
                    _tally(_upload_one(i, entry, change_type, progress, task_id))
    else:
        # Quiet or daemon mode — no progress bar.
        if concurrency > 1 and len(files_to_upload) > 1:
            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = {
                    pool.submit(_upload_one, i, entry, ct, None, None): (entry, ct)
                    for i, (entry, ct) in enumerate(files_to_upload, 1)
                }
                for future in as_completed(futures):
                    _tally(future.result())
        else:
            for i, (entry, change_type) in enumerate(files_to_upload, 1):
                _tally(_upload_one(i, entry, change_type, None, None))

    return result


def _echo_file_entry(entry: dict, prefix: str, color: str) -> None:
    """Print a file entry with color."""
    path = entry.get("path", "")
    filename = entry["filename"]
    display = f"{path}/{filename}" if path else filename
    click.echo(click.style(f"  {prefix} {display}", fg=color))
