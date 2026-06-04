"""oikb CLI — command-line interface for Open WebUI Knowledge Base sync."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import click

from oikb import __version__
from oikb.config import get_config, resolve_token, resolve_url, set_config


@click.group()
@click.version_option(version=__version__, prog_name="oikb")
@click.option("-q", "--quiet", is_flag=True, default=False, help="Suppress non-error output.")
@click.pass_context
def cli(ctx: click.Context, quiet: bool):
    """oikb — sync content to Open WebUI Knowledge Bases."""
    ctx.ensure_object(dict)
    ctx.obj["quiet"] = quiet


# ── Common options ──────────────────────────────────────────────

def common_options(f):
    """Shared --url, --token, --kb-id options."""
    f = click.option("--url", default=None, envvar="OPEN_WEBUI_URL", help="Open WebUI base URL.")(f)
    f = click.option("--token", default=None, envvar="OPEN_WEBUI_API_KEY", help="API key.")(f)
    f = click.option("--kb-id", "kb", default=None, help="Knowledge Base ID.")(f)
    return f


def _make_client(url: str | None, token: str | None):
    """Create an OikbClient from resolved config."""
    from oikb.client import OikbClient

    return OikbClient(
        base_url=resolve_url(url),
        token=resolve_token(token),
    )


def _resolve_connector(source: str, branch: str | None = None, path: str | None = None):
    """Resolve a source string to the appropriate connector."""
    if source.startswith("github:"):
        from oikb.connectors.github import GitHubConnector, parse_github_source
        parsed = parse_github_source(source)
        return GitHubConnector(owner=parsed["owner"], repo=parsed["repo"], branch=branch, path=path or parsed.get("path"))

    if source.startswith("gitlab:"):
        from oikb.connectors.gitlab import GitLabConnector, parse_gitlab_source
        parsed = parse_gitlab_source(source)
        return GitLabConnector(owner=parsed["owner"], repo=parsed["repo"], branch=branch, path=path or parsed.get("path"))

    if source.startswith("s3://"):
        from oikb.connectors.s3 import S3Connector, parse_s3_source
        parsed = parse_s3_source(source)
        return S3Connector(bucket=parsed["bucket"], prefix=path or parsed.get("prefix"))

    if source.startswith("gs://"):
        from oikb.connectors.gcs import GCSConnector, parse_gcs_source
        parsed = parse_gcs_source(source)
        return GCSConnector(bucket=parsed["bucket"], prefix=path or parsed.get("prefix"))

    if source.startswith("az://"):
        from oikb.connectors.azure_blob import AzureBlobConnector, parse_azure_source
        parsed = parse_azure_source(source)
        return AzureBlobConnector(container=parsed["container"], prefix=path or parsed.get("prefix"))

    if source.startswith("dropbox:"):
        from oikb.connectors.dropbox import DropboxConnector, parse_dropbox_source
        parsed = parse_dropbox_source(source)
        return DropboxConnector(path=parsed["path"])

    if source.startswith("gdrive:"):
        from oikb.connectors.gdrive import GDriveConnector, parse_gdrive_source
        parsed = parse_gdrive_source(source)
        return GDriveConnector(folder_id=parsed["folder_id"])

    if source.startswith("confluence:"):
        from oikb.connectors.confluence import ConfluenceConnector, parse_confluence_source
        parsed = parse_confluence_source(source)
        return ConfluenceConnector(space_key=parsed["space_key"], base_url=parsed.get("base_url"))

    if source.startswith("notion:"):
        from oikb.connectors.notion import NotionConnector, parse_notion_source
        parsed = parse_notion_source(source)
        return NotionConnector(root_id=parsed["root_id"])

    if source.startswith("slack:"):
        from oikb.connectors.slack import SlackConnector, parse_slack_source
        parsed = parse_slack_source(source)
        return SlackConnector(channel_id=parsed["channel_id"])

    if source.startswith("jira:"):
        from oikb.connectors.jira import JiraConnector, parse_jira_source
        parsed = parse_jira_source(source)
        fields = parsed["fields"].split(",") if parsed.get("fields") else None
        return JiraConnector(
            project_key=parsed["project_key"],
            jql=parsed.get("jql"),
            fields=fields,
            fmt=parsed.get("format", "markdown"),
            limit=int(parsed["limit"]) if parsed.get("limit") else None,
        )

    if source.startswith("sharepoint:"):
        from oikb.connectors.sharepoint import SharePointConnector, parse_sharepoint_source
        parsed = parse_sharepoint_source(source)
        return SharePointConnector(site=parsed["site"], library=parsed.get("library", "Documents"))

    if source.startswith("nextcloud:"):
        from oikb.connectors.nextcloud import NextcloudConnector, parse_nextcloud_source
        parsed = parse_nextcloud_source(source)
        return NextcloudConnector(root=parsed["root"])

    if source.startswith("web:"):
        from oikb.connectors.web import WebConnector, parse_web_source
        parsed = parse_web_source(source)
        return WebConnector(url=parsed["url"])

    if source.startswith("bitbucket:"):
        from oikb.connectors.bitbucket import BitbucketConnector, parse_bitbucket_source
        parsed = parse_bitbucket_source(source)
        return BitbucketConnector(owner=parsed["owner"], repo=parsed["repo"], branch=branch, path=path or parsed.get("path"))

    if source.startswith("discord:"):
        from oikb.connectors.discord import DiscordConnector, parse_discord_source
        parsed = parse_discord_source(source)
        return DiscordConnector(channel_id=parsed["channel_id"])

    if source.startswith("gmail:"):
        from oikb.connectors.gmail import GmailConnector, parse_gmail_source
        parsed = parse_gmail_source(source)
        return GmailConnector(user_email=parsed["user_email"], query=parsed.get("query", ""))

    if source.startswith("teams:"):
        from oikb.connectors.teams import TeamsConnector, parse_teams_source
        parsed = parse_teams_source(source)
        return TeamsConnector(team_id=parsed["team_id"], channel_id=parsed["channel_id"])

    if source.startswith("linear:"):
        from oikb.connectors.linear import LinearConnector, parse_linear_source
        parsed = parse_linear_source(source)
        return LinearConnector(team_key=parsed["team_key"])

    if source.startswith("zendesk:"):
        from oikb.connectors.zendesk import ZendeskConnector, parse_zendesk_source
        parsed = parse_zendesk_source(source)
        return ZendeskConnector(subdomain=parsed.get("subdomain"))

    if source.startswith("hubspot:"):
        from oikb.connectors.hubspot import HubSpotConnector
        return HubSpotConnector()

    if source.startswith("salesforce:"):
        from oikb.connectors.salesforce import SalesforceConnector
        return SalesforceConnector()

    if source.startswith("bookstack:"):
        from oikb.connectors.bookstack import BookStackConnector
        return BookStackConnector()

    if source.startswith("discourse:"):
        from oikb.connectors.discourse import DiscourseConnector, parse_discourse_source
        parsed = parse_discourse_source(source)
        return DiscourseConnector(category=parsed.get("category"))

    if source.startswith("airtable:"):
        from oikb.connectors.airtable import AirtableConnector, parse_airtable_source
        parsed = parse_airtable_source(source)
        return AirtableConnector(base_id=parsed["base_id"], table_name=parsed.get("table_name", "Table 1"))

    if source.startswith("freshdesk:"):
        from oikb.connectors.freshdesk import FreshdeskConnector, parse_freshdesk_source
        parsed = parse_freshdesk_source(source)
        return FreshdeskConnector(domain=parsed.get("domain"))

    if source.startswith("asana:"):
        from oikb.connectors.asana import AsanaConnector, parse_asana_source
        parsed = parse_asana_source(source)
        return AsanaConnector(project_id=parsed["project_id"])

    if source.startswith("clickup:"):
        from oikb.connectors.clickup import ClickUpConnector, parse_clickup_source
        parsed = parse_clickup_source(source)
        return ClickUpConnector(space_id=parsed["space_id"])

    if source.startswith("gitbook:"):
        from oikb.connectors.gitbook import GitBookConnector, parse_gitbook_source
        parsed = parse_gitbook_source(source)
        return GitBookConnector(space_id=parsed["space_id"])

    if source.startswith("guru:"):
        from oikb.connectors.guru import GuruConnector, parse_guru_source
        parsed = parse_guru_source(source)
        return GuruConnector(collection=parsed.get("collection"))

    if source.startswith("r2://"):
        from oikb.connectors.r2 import R2Connector, parse_r2_source
        parsed = parse_r2_source(source)
        return R2Connector(bucket=parsed["bucket"], prefix=parsed.get("prefix"))

    if source.startswith("document360:"):
        from oikb.connectors.document360 import Document360Connector, parse_document360_source
        parsed = parse_document360_source(source)
        return Document360Connector(project_id=parsed["project_id"])

    if source.startswith("slab:"):
        from oikb.connectors.slab import SlabConnector, parse_slab_source
        parsed = parse_slab_source(source)
        return SlabConnector(org=parsed.get("org"))

    if source.startswith("outline:"):
        from oikb.connectors.outline import OutlineConnector, parse_outline_source
        parsed = parse_outline_source(source)
        return OutlineConnector(collection=parsed.get("collection"))

    if source.startswith("gsites:"):
        from oikb.connectors.google_sites import GoogleSitesConnector, parse_gsites_source
        parsed = parse_gsites_source(source)
        return GoogleSitesConnector(site_id=parsed["site_id"])

    if source.startswith("egnyte:"):
        from oikb.connectors.egnyte import EgnyteConnector, parse_egnyte_source
        parsed = parse_egnyte_source(source)
        return EgnyteConnector(path=parsed.get("path", "/"))

    if source.startswith("oci://"):
        from oikb.connectors.oracle_storage import OracleStorageConnector, parse_oracle_source
        parsed = parse_oracle_source(source)
        return OracleStorageConnector(bucket=parsed["bucket"], prefix=parsed.get("prefix"))

    if source.startswith("productboard:"):
        from oikb.connectors.productboard import ProductBoardConnector
        return ProductBoardConnector()

    if source.startswith("xenforo:"):
        from oikb.connectors.xenforo import XenForoConnector, parse_xenforo_source
        parsed = parse_xenforo_source(source)
        return XenForoConnector(forum_id=parsed.get("forum_id"))

    if source.startswith("zulip:"):
        from oikb.connectors.zulip import ZulipConnector, parse_zulip_source
        parsed = parse_zulip_source(source)
        return ZulipConnector(stream=parsed.get("stream"))

    if source.startswith("gong:"):
        from oikb.connectors.gong import GongConnector
        return GongConnector()

    if source.startswith("fireflies:"):
        from oikb.connectors.fireflies import FirefliesConnector
        return FirefliesConnector()

    if source.startswith("dokuwiki:"):
        from oikb.connectors.dokuwiki import DokuWikiConnector, parse_dokuwiki_source
        parsed = parse_dokuwiki_source(source)
        return DokuWikiConnector(namespace=parsed.get("namespace"))

    if source.startswith("servicenow:"):
        from oikb.connectors.servicenow import ServiceNowConnector, parse_servicenow_source
        parsed = parse_servicenow_source(source)
        fields = parsed["fields"].split(",") if parsed.get("fields") else None
        return ServiceNowConnector(
            table=parsed.get("table", "incident"),
            query=parsed.get("query"),
            fields=fields,
            fmt=parsed.get("format", "markdown"),
            limit=int(parsed.get("limit", "1000")),
        )

    # Default: local filesystem.
    from oikb.connectors.filesystem import FilesystemConnector
    return FilesystemConnector(source)


def _interpolate_env(obj: Any) -> Any:
    """Recursively interpolate ${VAR} and ${VAR:-default} in string values."""
    import re

    _ENV_RE = re.compile(r"\$\{([^}]+)\}")

    def _replace(match: re.Match) -> str:
        expr = match.group(1)
        if ":-" in expr:
            var, default = expr.split(":-", 1)
            return os.environ.get(var, default)
        return os.environ.get(expr, match.group(0))

    if isinstance(obj, str):
        return _ENV_RE.sub(_replace, obj)
    if isinstance(obj, dict):
        return {k: _interpolate_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_interpolate_env(item) for item in obj]
    return obj


def _deep_merge(base: dict, override: dict) -> dict:
    """Deep merge two dicts. Values in override take precedence."""
    result = base.copy()
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _load_oikb_yaml() -> list[dict] | None:
    """Load .oikb.yaml from the current directory if it exists."""
    import yaml

    yaml_path = Path(".oikb.yaml")
    if not yaml_path.exists():
        return None

    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    if not data:
        return None

    # Interpolate environment variables in all string values.
    data = _interpolate_env(data)

    # Prefer sources: (new), fall back to sync: (legacy).
    entries = data.get("sources") or data.get("sync")
    if not entries:
        return None

    # Apply global defaults to each entry.
    defaults = data.get("defaults", {})
    if defaults:
        entries = [_deep_merge(defaults, entry) for entry in entries]

    return entries


def _build_cli_filter(max_file_size: str | None):
    """Build a manifest filter from CLI-only flags (no yaml)."""
    if not max_file_size:
        return None
    from oikb.sync import build_manifest_filter, parse_size
    return build_manifest_filter(max_size=parse_size(max_file_size))


# ── sync ────────────────────────────────────────────────────────

@cli.command()
@click.argument("source", required=False)
@common_options
@click.option("--branch", default=None, help="Branch for GitHub sources.")
@click.option("--path", "source_path", default=None, help="Subdirectory within the source.")
@click.option("--dry-run", is_flag=True, help="Preview changes without uploading.")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed progress.")
@click.option("--name", default=None, help="Target a specific entry in .oikb.yaml by name/kb-id.")
@click.option("--concurrency", default=1, type=int, help="Parallel upload workers (default: 1, sequential).")
@click.option("--max-file-size", default=None, help="Skip files larger than this (e.g. 50mb, 1gb).")
@click.pass_context
def sync(
    ctx: click.Context,
    source: str | None,
    url: str | None,
    token: str | None,
    kb: str | None,
    branch: str | None,
    source_path: str | None,
    dry_run: bool,
    verbose: bool,
    name: str | None,
    concurrency: int,
    max_file_size: str | None,
):
    """Incremental sync from a source to a Knowledge Base.

    SOURCE can be a local directory, github:owner/repo, or s3://bucket/prefix.
    If omitted, reads .oikb.yaml from the current directory.
    """
    quiet = ctx.obj.get("quiet", False)
    from oikb.sync import run_sync

    # ── .oikb.yaml mode ──
    if source is None:
        entries = _load_oikb_yaml()
        if not entries:
            click.echo(
                click.style("No source specified and no .oikb.yaml found.", fg="red"),
                err=True,
            )
            sys.exit(1)

        # Filter by --name if specified.
        if name:
            entries = [e for e in entries if e.get("name") == name or e.get("kb-id") == name]
            if not entries:
                click.echo(click.style(f"No entry matching '{name}' in .oikb.yaml", fg="red"), err=True)
                sys.exit(1)

        has_errors = False
        for entry in entries:
            entry_source = entry.get("source")
            entry_kb = entry.get("kb-id")
            entry_branch = entry.get("branch")
            entry_path = entry.get("path")
            entry_filter = entry.get("filter", {})

            if not entry_source or not entry_kb:
                click.echo(click.style(f"Skipping invalid entry (needs source + kb-id): {entry}", fg="yellow"), err=True)
                continue

            try:
                connector = _resolve_connector(entry_source, entry_branch, entry_path)
                client = _make_client(url, token)

                if not quiet:
                    click.echo(f"\n{'─' * 40}")
                    click.echo(f"Syncing: {entry_source} → {entry_kb}")

                mf = None
                inc = entry_filter.get("include")
                exc = entry_filter.get("exclude")
                ms = entry_filter.get("max-size") or max_file_size
                if inc or exc or ms:
                    from oikb.sync import build_manifest_filter, parse_size
                    mf = build_manifest_filter(
                        include=inc,
                        exclude=exc,
                        max_size=parse_size(ms),
                    )

                result = run_sync(
                    client=client,
                    connector=connector,
                    kb_id=entry_kb,
                    dry_run=dry_run,
                    verbose=verbose,
                    quiet=quiet,
                    manifest_filter=mf,
                    concurrency=entry.get("concurrency", concurrency),
                )

                if not quiet:
                    prefix = "Dry run" if dry_run else "Done"
                    click.echo(f"  {prefix}: {result.summary()}")

                if result.errors:
                    has_errors = True

                client.close()

            except Exception as e:
                click.echo(click.style(f"  Failed: {e}", fg="red"), err=True)
                has_errors = True

        if has_errors:
            sys.exit(1)
        return

    # ── Single source mode ──
    if not kb:
        click.echo(click.style("--kb-id is required when syncing a single source.", fg="red"), err=True)
        sys.exit(1)

    try:
        connector = _resolve_connector(source, branch, source_path)
    except (FileNotFoundError, ImportError, ValueError) as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

    try:
        client = _make_client(url, token)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)

    try:
        result = run_sync(
            client=client,
            connector=connector,
            kb_id=kb,
            dry_run=dry_run,
            verbose=verbose,
            quiet=quiet,
            concurrency=concurrency,
            manifest_filter=_build_cli_filter(max_file_size),
        )
    except Exception as e:
        click.echo(click.style(f"Sync failed: {e}", fg="red"), err=True)
        sys.exit(1)
    finally:
        client.close()

    if not quiet:
        prefix = "Dry run" if dry_run else "Sync complete"
        click.echo(f"{prefix}: {result.summary()}")

    if result.errors:
        click.echo(click.style(f"\n{len(result.errors)} error(s):", fg="red"), err=True)
        for err in result.errors:
            click.echo(f"  • {err}", err=True)
        sys.exit(1)


# ── diff ────────────────────────────────────────────────────────

@cli.command()
@click.argument("source")
@common_options
@click.option("--branch", default=None, help="Branch for GitHub sources.")
@click.option("--path", "source_path", default=None, help="Subdirectory within the source.")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed output.")
@click.pass_context
def diff(
    ctx: click.Context,
    source: str,
    url: str | None,
    token: str | None,
    kb: str | None,
    branch: str | None,
    source_path: str | None,
    verbose: bool,
):
    """Preview what a sync would do (alias for sync --dry-run)."""
    if not kb:
        click.echo(click.style("--kb-id is required.", fg="red"), err=True)
        sys.exit(1)

    from oikb.sync import run_sync

    try:
        connector = _resolve_connector(source, branch, source_path)
    except (FileNotFoundError, ImportError, ValueError) as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)

    try:
        client = _make_client(url, token)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)

    try:
        result = run_sync(
            client=client,
            connector=connector,
            kb_id=kb,
            dry_run=True,
            verbose=verbose,
        )
    except Exception as e:
        click.echo(click.style(f"Diff failed: {e}", fg="red"), err=True)
        sys.exit(1)
    finally:
        client.close()

    click.echo(f"\n{result.summary()}")


# ── watch ───────────────────────────────────────────────────────

@cli.command()
@click.argument("directory")
@common_options
@click.option("--debounce", default=1.0, type=float, help="Seconds to wait after last change (default: 1.0).")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed progress.")
@click.pass_context
def watch(
    ctx: click.Context,
    directory: str,
    url: str | None,
    token: str | None,
    kb: str | None,
    debounce: float,
    verbose: bool,
):
    """Watch a local directory and sync on changes.

    Runs continuously until interrupted (Ctrl+C).
    """
    if not kb:
        click.echo(click.style("--kb-id is required.", fg="red"), err=True)
        sys.exit(1)

    quiet = ctx.obj.get("quiet", False)

    from oikb.watcher import watch_directory
    from oikb.connectors.filesystem import FilesystemConnector, DEFAULT_IGNORE
    from oikb.sync import run_sync

    try:
        client = _make_client(url, token)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)

    click.echo(f"Watching {directory} → KB {kb} (Ctrl+C to stop)")

    def on_change():
        try:
            connector = FilesystemConnector(directory)
            result = run_sync(
                client=client,
                connector=connector,
                kb_id=kb,
                verbose=verbose,
                quiet=quiet,
            )
            if not quiet:
                click.echo(f"  [{_timestamp()}] {result.summary()}")
        except Exception as e:
            click.echo(click.style(f"  [{_timestamp()}] Sync error: {e}", fg="red"), err=True)

    try:
        watch_directory(
            directory=directory,
            on_change=on_change,
            debounce_seconds=debounce,
            ignore=DEFAULT_IGNORE,
        )
    except FileNotFoundError as e:
        click.echo(click.style(f"Error: {e}", fg="red"), err=True)
        sys.exit(1)
    finally:
        client.close()


# ── reset ───────────────────────────────────────────────────────

@cli.command()
@common_options
@click.option(
    "--keep-directories",
    is_flag=True,
    help="Keep directory structure, only remove files.",
)
@click.confirmation_option(
    prompt="This will delete all files in the Knowledge Base. Continue?"
)
def reset(url: str | None, token: str | None, kb: str | None, keep_directories: bool):
    """Reset a Knowledge Base (delete all files)."""
    if not kb:
        click.echo(click.style("--kb-id is required.", fg="red"), err=True)
        sys.exit(1)

    try:
        client = _make_client(url, token)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)

    try:
        client.reset_kb(kb, include_directories=not keep_directories)
    except Exception as e:
        click.echo(click.style(f"Reset failed: {e}", fg="red"), err=True)
        sys.exit(1)
    finally:
        client.close()

    click.echo("Knowledge Base reset.")


# ── ls ──────────────────────────────────────────────────────────

@cli.command()
@common_options
def ls(url: str | None, token: str | None, kb: str | None):
    """List files in a Knowledge Base."""
    if not kb:
        click.echo(click.style("--kb-id is required.", fg="red"), err=True)
        sys.exit(1)

    try:
        client = _make_client(url, token)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)

    try:
        files = client.list_kb_files(kb)
    except Exception as e:
        click.echo(click.style(f"Failed: {e}", fg="red"), err=True)
        sys.exit(1)
    finally:
        client.close()

    if not files:
        click.echo("(empty)")
        return

    for f in files:
        meta = f.get("meta", {})
        name = meta.get("name", f.get("filename", "?"))
        size = meta.get("size", 0)
        file_hash = meta.get("file_hash", "")[:8]
        click.echo(f"  {name}  ({_format_size(size)})  {file_hash}")


# ── status ──────────────────────────────────────────────────────

@cli.command()
@common_options
def status(url: str | None, token: str | None, kb: str | None):
    """Show Knowledge Base info and file count."""
    if not kb:
        click.echo(click.style("--kb-id is required.", fg="red"), err=True)
        sys.exit(1)

    try:
        client = _make_client(url, token)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"), err=True)
        sys.exit(1)

    try:
        info = client.get_kb(kb)
        files = info.get("files", [])
    except Exception as e:
        click.echo(click.style(f"Failed: {e}", fg="red"), err=True)
        sys.exit(1)
    finally:
        client.close()

    name = info.get("name", kb)
    description = info.get("description", "")
    file_count = len(files)
    total_size = sum(f.get("meta", {}).get("size", 0) for f in files)

    click.echo(f"Knowledge Base: {name}")
    if description:
        click.echo(f"  Description: {description}")
    click.echo(f"  ID:          {kb}")
    click.echo(f"  Files:       {file_count}")
    click.echo(f"  Total size:  {_format_size(total_size)}")

    updated_at = info.get("updated_at")
    if updated_at:
        click.echo(f"  Updated:     {updated_at}")


# ── config ──────────────────────────────────────────────────────

@cli.group()
def config():
    """Manage oikb configuration."""


@config.command("set")
@click.argument("key", type=click.Choice(["url", "token"]))
@click.argument("value")
def config_set(key: str, value: str):
    """Set a config value (url or token)."""
    set_config(key, value)
    display = value if key == "url" else f"{value[:4]}...{value[-4:]}"
    click.echo(f"{key} = {display}")


@config.command("get")
@click.argument("key", required=False)
def config_get(key: str | None):
    """Show config values."""
    data = get_config(key)
    if isinstance(data, dict):
        if not data:
            click.echo("(no config set)")
            return
        for k, v in data.items():
            display = v if k == "url" else f"{v[:4]}...{v[-4:]}" if v else ""
            click.echo(f"{k} = {display}")
    elif data:
        click.echo(data)
    else:
        click.echo("(not set)")


# ── init ────────────────────────────────────────────────────────

@cli.command()
@click.option("--force", is_flag=True, help="Overwrite existing .oikb.yaml.")
def init(force: bool):
    """Generate a .oikb.yaml config file interactively.

    \b
    Asks for source, Knowledge Base ID, name, and interval,
    then writes a ready-to-use config file.
    """
    yaml_path = Path(".oikb.yaml")
    if yaml_path.exists() and not force:
        click.echo(click.style(f"{yaml_path} already exists. Use --force to overwrite.", fg="red"), err=True)
        sys.exit(1)

    click.echo(click.style("oikb init — generate .oikb.yaml\n", bold=True))

    entries: list[dict] = []
    while True:
        click.echo(click.style(f"─ Source #{len(entries) + 1}", fg="cyan"))

        source = click.prompt(
            "  Source (e.g. ./docs, github:owner/repo, confluence:SPACE)",
            type=str,
        )
        kb_id = click.prompt("  Knowledge Base ID", type=str)
        name = click.prompt("  Name (short alias)", default=source.split(":")[-1].split("/")[-1], type=str)
        interval = click.prompt("  Sync interval (daemon mode)", default="1h", type=str)

        entries.append({
            "name": name,
            "source": source,
            "kb-id": kb_id,
            "interval": interval,
        })

        if not click.confirm("\n  Add another source?", default=False):
            break
        click.echo()

    import yaml

    data: dict = {"sources": entries}
    yaml_path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))

    click.echo(f"\n{click.style('✓', fg='green')} Wrote {yaml_path}")
    click.echo(f"\n  {click.style('Next steps:', bold=True)}")
    click.echo(f"  oikb validate        # check your config")
    click.echo(f"  oikb sync            # run a one-time sync")
    click.echo(f"  oikb sync --dry-run  # preview without uploading")
    click.echo(f"  oikb daemon          # start scheduled sync")


# ── validate ────────────────────────────────────────────────────

@cli.command()
@click.option("--config", "config_file", default=None, type=click.Path(), help="Path to .oikb.yaml (default: ./.oikb.yaml).")
@click.option("--deep", is_flag=True, help="Verify connectivity: ping Open WebUI, check API key, confirm each KB exists.")
def validate(config_file: str | None, deep: bool):
    """Validate .oikb.yaml without running anything.

    By default, checks YAML structure and source syntax.
    With --deep, also verifies the Open WebUI API is reachable,
    the API key is valid, and each Knowledge Base ID exists.
    """
    if config_file:
        import yaml
        with open(config_file) as f:
            data = yaml.safe_load(f)
        data = _interpolate_env(data) if data else data
        entries = (data.get("sources") or data.get("sync", [])) if data else []
        defaults = data.get("defaults", {}) if data else {}
        if defaults and entries:
            entries = [_deep_merge(defaults, e) for e in entries]
    else:
        entries = _load_oikb_yaml()

    if not entries:
        click.echo(click.style("No .oikb.yaml found or file is empty.", fg="red"), err=True)
        sys.exit(1)

    # Deep validation: create a client and verify connectivity first.
    client = None
    if deep:
        click.echo(click.style("Deep validation\n", bold=True))
        try:
            client = _make_client(
                url=entries[0].get("url"),
                token=entries[0].get("token"),
            )
            click.echo(click.style("  ✓ API reachable", fg="green") + f"  {client._http.base_url}")
        except Exception as e:
            click.echo(click.style(f"  ✗ Cannot reach Open WebUI: {e}", fg="red"))
            sys.exit(1)

    has_errors = False
    for i, entry in enumerate(entries, 1):
        entry_name = entry.get("name", f"entry #{i}")
        source = entry.get("source")
        kb_id = entry.get("kb-id")

        if not source or not kb_id:
            click.echo(click.style(f"  ✗ {entry_name}: missing source or kb-id", fg="red"))
            has_errors = True
            continue

        # Syntax check: resolve the connector.
        try:
            _resolve_connector(source)
        except Exception as e:
            click.echo(click.style(f"  ✗ {entry_name}: {e}", fg="red"))
            has_errors = True
            continue

        # Deep check: verify the KB exists.
        if deep and client:
            try:
                kb = client.get_kb(kb_id)
                kb_name = kb.get("name", "?")
                file_count = len(kb.get("files", []))
                click.echo(
                    click.style(f"  ✓ {entry_name}", fg="green")
                    + f"  {source} → {kb_name} ({file_count} files)"
                )
            except Exception as e:
                click.echo(click.style(f"  ✗ {entry_name}: KB {kb_id} — {e}", fg="red"))
                has_errors = True
        else:
            click.echo(click.style(f"  ✓ {entry_name}", fg="green") + f"  {source} → {kb_id}")

    if client:
        client.close()

    if has_errors:
        sys.exit(1)
    else:
        click.echo(click.style(f"\n{len(entries)} entry(s) valid.", fg="green"))


# ── daemon ──────────────────────────────────────────────────────

@cli.command()
@click.option("--port", default=8080, type=int, help="HTTP port for healthcheck and API (default: 8080).")
@click.option("--no-server", is_flag=True, help="Run scheduler only, no HTTP server.")
@click.option("--config", "config_file", default=None, type=click.Path(), help="Path to .oikb.yaml (default: ./.oikb.yaml).")
@click.option("--log-format", default=None, type=click.Choice(["text", "json"]), help="Log output format (default: text, env: LOG_FORMAT).")
def daemon(port: int, no_server: bool, config_file: str | None, log_format: str | None):
    """Run as a long-lived daemon with scheduled sync.

    Reads .oikb.yaml and syncs each source on its configured interval.
    Exposes /health, /history, and /sync endpoints.
    """
    log_format = log_format or os.environ.get("LOG_FORMAT", "text")

    if config_file:
        import yaml
        with open(config_file) as f:
            data = yaml.safe_load(f)
        data = _interpolate_env(data) if data else data
        entries = (data.get("sources") or data.get("sync", [])) if data else []
        defaults = data.get("defaults", {}) if data else {}
        if defaults and entries:
            entries = [_deep_merge(defaults, e) for e in entries]
    else:
        entries = _load_oikb_yaml()

    if not entries:
        click.echo(click.style("No sync entries found. Create a .oikb.yaml file.", fg="red"), err=True)
        sys.exit(1)

    # Validate entries.
    for entry in entries:
        if not entry.get("source") or not entry.get("kb-id"):
            click.echo(click.style(f"Invalid entry (needs source + kb-id): {entry}", fg="red"), err=True)
            sys.exit(1)

    from oikb.daemon import start_daemon
    start_daemon(entries=entries, port=port, no_server=no_server, log_format=log_format)


# ── history ─────────────────────────────────────────────────────

@cli.command()
@click.option("--limit", default=20, type=int, help="Number of entries to show.")
@click.option("--kb-id", default=None, help="Filter by Knowledge Base ID.")
@click.option("--errors", is_flag=True, help="Show only failed syncs.")
@click.option("--clear", is_flag=True, help="Clear old entries.")
@click.option("--days", default=30, type=int, help="Clear entries older than N days (with --clear).")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def history(limit: int, kb_id: str | None, errors: bool, clear: bool, days: int, as_json: bool):
    """View sync history."""
    import json as json_mod
    from oikb.history import SyncHistory

    hist = SyncHistory()

    if clear:
        count = hist.clear(older_than_days=days)
        click.echo(f"Cleared {count} entries older than {days} days.")
        hist.close()
        return

    entries = hist.query(limit=limit, kb_id=kb_id, errors_only=errors)
    hist.close()

    if as_json:
        click.echo(json_mod.dumps(entries, indent=2, default=str))
        return

    if not entries:
        click.echo("No sync history.")
        return

    # Table output.
    click.echo(f"{'SOURCE':<30} {'KB-ID':<20} {'STATUS':<10} {'DURATION':<10} {'FILES':<12} {'TIME'}")
    click.echo("-" * 100)
    for e in entries:
        source = (e["source"] or "")[:28]
        kb = (e["kb_id"] or "")[:18]
        status = e["status"]
        duration = f"{e.get('duration_ms', 0)}ms" if e.get("duration_ms") else "--"
        added = e.get("files_added", 0)
        modified = e.get("files_modified", 0)
        deleted = e.get("files_deleted", 0)
        files = f"+{added} ~{modified} -{deleted}"
        ago = _time_ago(e.get("started_at", 0))

        color = "green" if status == "success" else "red" if status == "error" else "yellow"
        click.echo(
            f"{source:<30} {kb:<20} "
            + click.style(f"{status:<10}", fg=color)
            + f"{duration:<10} {files:<12} {ago}"
        )
        if status == "error" and e.get("error_message"):
            click.echo(click.style(f"  Error: {e['error_message']}", fg="red"))


# ── Helpers ─────────────────────────────────────────────────────

def _format_size(size: int) -> str:
    """Human-readable file size."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _timestamp() -> str:
    """Current time as HH:MM:SS."""
    from datetime import datetime

    return datetime.now().strftime("%H:%M:%S")


def _time_ago(ts: float) -> str:
    """Human-readable time since a Unix timestamp."""
    import time
    delta = int(time.time() - ts)
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    if delta < 86400:
        return f"{delta // 3600}h ago"
    return f"{delta // 86400}d ago"
