"""oikb daemon -- FastAPI server with built-in scheduler."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Optional

import click
import uvicorn
from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from oikb import __version__
from oikb.env import API_KEY
from oikb.history import SyncHistory
from oikb.metrics import record_sync, set_build_info

log = logging.getLogger(__name__)

# ── Auth dependency ──────────────────────────────────────────────

bearer_scheme = HTTPBearer(auto_error=False)


async def verify_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
):
    if not API_KEY:
        return
    if not credentials or not hmac.compare_digest(credentials.credentials, API_KEY):
        raise HTTPException(status_code=401, detail="Invalid API key")


app = FastAPI(
    title="oikb",
    description="Sync engine for Open WebUI Knowledge Bases. Trigger syncs, check status, and query history.",
    version="0.3.5",
)

# Runtime state populated by start_daemon().
_scheduler_state: dict[str, dict[str, Any]] = {}
_sync_locks: dict[str, asyncio.Lock] = {}
_history: SyncHistory | None = None
_entries: list[dict] = []
_shutdown_event: asyncio.Event | None = None


# ── Interval / cron parser ───────────────────────────────────────


def _is_cron(value: str) -> bool:
    """Check if a string looks like a cron expression (5-6 space-separated fields)."""
    return isinstance(value, str) and len(value.strip().split()) >= 5


def parse_interval(value: str | int) -> int:
    """Parse an interval string like '5m', '1h', '30s' to seconds."""
    if isinstance(value, (int, float)):
        return int(value)
    value = value.strip().lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if value[-1] in multipliers:
        return int(value[:-1]) * multipliers[value[-1]]
    return int(value)


def _next_cron_delay(cron_expr: str) -> float:
    """Compute seconds until the next cron fire time."""
    from datetime import datetime

    from croniter import croniter

    now = datetime.now()
    cron = croniter(cron_expr, now)
    next_time = cron.get_next(datetime)
    return max((next_time - now).total_seconds(), 1.0)


# ── Dashboard ────────────────────────────────────────────────────

from fastapi.responses import HTMLResponse

_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>oikb</title>
<style>
body{font-family:monospace;background:#111;color:#ccc;padding:2rem;font-size:14px}
h1{font-size:16px;margin-bottom:1rem;color:#fff}
.row{padding:.5rem 0;border-bottom:1px solid #222;display:flex;gap:1rem;align-items:baseline}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;flex-shrink:0;margin-top:3px}
.ok{background:#22c55e}.run{background:#3b82f6}.err{background:#ef4444}.warn{background:#f59e0b}.idle{background:#555}
.name{color:#fff;min-width:120px}.dim{color:#666}
.error{color:#ef4444;font-size:12px;margin-left:28px;margin-top:2px}
</style>
</head>
<body>
<h1>oikb <span id="v" style="color:#666"></span></h1>
<div id="out">loading...</div>
<script>
function ago(t){if(!t)return'-';const s=Math.floor(Date.now()/1000-t);if(s<60)return s+'s ago';if(s<3600)return(s/60|0)+'m ago';if(s<86400)return(s/3600|0)+'h ago';return(s/86400|0)+'d ago'}
async function poll(){
 try{
  const d=await(await fetch('/health')).json();
  document.getElementById('v').textContent=d.version||'';
  const S=d.sources||{};const K=Object.keys(S);
  if(!K.length){document.getElementById('out').textContent='no sources configured';return}
  document.getElementById('out').innerHTML=K.map(k=>{
   const s=S[k],st=s.status||'idle';
   const c=st==='success'?'ok':st==='running'?'run':st==='error'?'err':st==='partial'?'warn':'idle';
   const e=s.errors&&s.errors.length?'<div class="error">'+s.errors[0]+'</div>':'';
   return '<div class="row"><span class="dot '+c+'"></span><span class="name">'+(s.name||k)+'</span>'
    +'<span class="dim">'+ago(s.last_sync)+'</span>'
    +'<span class="dim">'+(s.duration_ms?s.duration_ms+'ms':'-')+'</span>'
    +'<span class="dim">+'+( s.files_added||0)+' ~'+(s.files_modified||0)+' -'+(s.files_deleted||0)+'</span>'
    +'<span class="dim">'+(s.next_sync_in||'')+'</span></div>'+e
  }).join('');
 }catch(e){}
}
poll();setInterval(poll,10000);
</script>
</body>
</html>"""


# ── FastAPI routes ───────────────────────────────────────────────

@app.get("/", include_in_schema=False, response_class=HTMLResponse)
async def dashboard():
    """Status dashboard — polls /health and renders source statuses."""
    return _DASHBOARD_HTML


@app.get(
    "/health",
    operation_id="get_sync_status",
    summary="Get sync status for all configured sources",
)
async def health():
    """Returns the current sync status for every configured source, including last sync time, duration, file counts, and any errors. Use this to check if syncs are running and healthy."""
    return {
        "status": "ok",
        "version": __version__,
        "sources": _scheduler_state,
    }


@app.get("/health/ready", include_in_schema=False)
async def ready():
    """Liveness probe."""
    return {"ready": True}


@app.get(
    "/history",
    operation_id="get_sync_history",
    summary="Query sync history log",
    dependencies=[Depends(verify_api_key)],
)
async def history_endpoint(
    limit: int = 50,
    kb_id: str | None = None,
    errors_only: bool = False,
):
    """Returns recent sync history entries from the local database. Filter by Knowledge Base ID or show only errors. Each entry includes source, status, duration, and file change counts."""
    if not _history:
        return {"entries": []}
    entries = await asyncio.to_thread(
        _history.query, limit=limit, kb_id=kb_id, errors_only=errors_only,
    )
    return {"entries": entries}


@app.post(
    "/sync/{identifier}",
    operation_id="trigger_sync",
    summary="Trigger an immediate sync by alias or KB ID",
    dependencies=[Depends(verify_api_key)],
)
async def trigger_sync(identifier: str, dry_run: bool = False):
    """Triggers an immediate sync matching the given alias or Knowledge Base ID. The sync runs asynchronously in the background. Use get_sync_status to check progress. Set dry_run=true to preview changes without uploading."""
    for entry in _entries:
        if entry.get("name") == identifier or entry.get("kb-id") == identifier:
            if dry_run:
                result = await _run_entry(entry, dry_run=True)
                return {"dry_run": True, "name": entry.get("name"), "kb_id": entry.get("kb-id"), "result": result}
            asyncio.create_task(_run_entry(entry))
            return {"triggered": True, "name": entry.get("name"), "kb_id": entry.get("kb-id")}
    return {"triggered": False, "error": f"No entry matching '{identifier}'"}


# ── Scheduler ────────────────────────────────────────────────────


async def _send_notification(entry: dict, payload: dict) -> None:
    """Fire a webhook notification if configured on this entry."""
    notify = entry.get("notify")
    if not notify:
        return

    url = notify if isinstance(notify, str) else notify.get("url")
    if not url:
        return

    trigger_on = notify.get("on", "error") if isinstance(notify, dict) else "error"
    status = payload.get("status", "")

    # Only fire if the trigger condition matches.
    if trigger_on == "error" and status not in ("error", "partial"):
        return

    import httpx as _httpx

    # Include a human-readable `text` field for Slack compatibility.
    source = payload.get("source", "unknown")
    if status == "error":
        text = f"❌ oikb sync failed: {source}\n{payload.get('error', '')}"
    elif status == "partial":
        errors = payload.get("errors", [])
        text = f"⚠️ oikb sync partial: {source} — {len(errors)} error(s)"
    else:
        text = f"✅ oikb sync OK: {source} — {payload.get('summary', '')}"

    payload["text"] = text

    try:
        async with _httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except Exception as exc:
        log.warning(f"Notification failed for {source}: {exc}")


async def _run_entry(entry: dict, dry_run: bool = False) -> dict | None:
    """Run a single sync for an entry.

    Uses a per-KB lock to prevent overlapping syncs to the same
    Knowledge Base (e.g. webhook fires while a scheduled sync is running).
    """
    kb_id = entry["kb-id"]

    # Get or create a lock for this KB.
    if kb_id not in _sync_locks:
        _sync_locks[kb_id] = asyncio.Lock()
    lock = _sync_locks[kb_id]

    if lock.locked():
        log.info(f"Skipping {entry.get('source', '?')} — sync already running for {kb_id}")
        return {"skipped": True, "reason": "sync already running"} if dry_run else None

    async with lock:
        return await _run_entry_locked(entry, dry_run=dry_run)


async def _run_entry_locked(entry: dict, dry_run: bool = False) -> dict | None:
    """Inner sync logic, called under the per-KB lock."""
    from oikb.cli import _make_client, _resolve_connector
    from oikb.sync import run_sync

    source = entry["source"]
    kb_id = entry["kb-id"]
    started_at = time.time()

    _scheduler_state[source] = {
        **_scheduler_state.get(source, {}),
        "name": entry.get("name", source),
        "status": "running",
        "started_at": started_at,
    }

    try:
        connector = _resolve_connector(
            source,
            branch=entry.get("branch"),
            path=entry.get("path"),
        )
        client = _make_client(
            url=entry.get("url"),
            token=entry.get("token"),
        )

        mf = None
        entry_filter = entry.get("filter", {})
        inc = entry_filter.get("include")
        exc = entry_filter.get("exclude")
        ms = entry_filter.get("max-size")
        if inc or exc or ms:
            from oikb.sync import build_manifest_filter, parse_size
            mf = build_manifest_filter(
                include=inc,
                exclude=exc,
                max_size=parse_size(ms),
            )

        result = await asyncio.to_thread(
            run_sync,
            client=client,
            connector=connector,
            kb_id=kb_id,
            dry_run=dry_run,
            quiet=True,
            manifest_filter=mf,
            concurrency=entry.get("concurrency", 1),
        )
        client.close()

        if dry_run:
            return {
                "added": result.added,
                "modified": result.modified,
                "deleted": result.deleted,
                "unmodified": result.unmodified,
                "summary": result.summary(),
            }

        duration_s = time.time() - started_at
        duration_ms = int(duration_s * 1000)
        status = "success" if not result.errors else "partial"

        _scheduler_state[source] = {
            "name": entry.get("name", source),
            "status": status,
            "last_sync": time.time(),
            "duration_ms": duration_ms,
            "files_added": result.added,
            "files_modified": result.modified,
            "files_deleted": result.deleted,
            "unmodified": result.unmodified,
            "errors": result.errors or [],
        }

        record_sync(
            source=source,
            status=status,
            duration_seconds=duration_s,
            files_added=result.added,
            files_modified=result.modified,
            files_deleted=result.deleted,
        )

        if _history:
            await asyncio.to_thread(
                _history.log,
                source=source,
                kb_id=kb_id,
                status="success",
                started_at=started_at,
                files_added=result.added,
                files_modified=result.modified,
                files_deleted=result.deleted,
                unmodified=result.unmodified,
            )

        log.info(
            f"Synced {source} -> {kb_id}: {result.summary()} ({duration_ms}ms)"
        )

        await _send_notification(entry, {
            "source": source,
            "kb_id": kb_id,
            "status": status,
            "duration_ms": duration_ms,
            "summary": result.summary(),
            "files_added": result.added,
            "files_modified": result.modified,
            "files_deleted": result.deleted,
            "errors": result.errors or [],
        })

    except Exception as e:
        _scheduler_state[source] = {
            "name": entry.get("name", source),
            "status": "error",
            "last_sync": time.time(),
            "error": str(e),
        }
        record_sync(
            source=source,
            status="error",
            duration_seconds=time.time() - started_at,
        )
        if _history:
            await asyncio.to_thread(
                _history.log,
                source=source,
                kb_id=kb_id,
                status="error",
                started_at=started_at,
                error=str(e),
            )
        log.error(f"Sync failed for {source}: {e}")

        await _send_notification(entry, {
            "source": source,
            "kb_id": kb_id,
            "status": "error",
            "error": str(e),
        })


async def _schedule_entry(entry: dict) -> None:
    """Run sync for one .oikb.yaml entry on a loop.

    Supports both simple intervals ('30m', '1h') and cron expressions
    ('0 */6 * * *', '0 6 * * 1-5').
    """
    raw_interval = entry.get("interval", "30m")
    source = entry.get("source", "unknown")
    use_cron = _is_cron(str(raw_interval))

    if use_cron:
        log.info(f"Scheduling {source} with cron: {raw_interval}")
    else:
        interval = parse_interval(raw_interval)
        log.info(f"Scheduling {source} every {interval}s")

    while not _shutdown_event.is_set():
        await _run_entry(entry)

        # Compute wait until next run.
        if use_cron:
            delay = _next_cron_delay(str(raw_interval))
            _scheduler_state.setdefault(source, {})["next_sync_in"] = f"{int(delay)}s"
        else:
            delay = float(interval)
            _scheduler_state.setdefault(source, {})["next_sync_in"] = f"{interval}s"

        try:
            await asyncio.wait_for(_shutdown_event.wait(), timeout=delay)
            break  # Shutdown requested.
        except asyncio.TimeoutError:
            pass  # Time elapsed, run again.


async def _run_scheduler(entries: list[dict]) -> None:
    """Start all sync tasks and wait for shutdown."""
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _shutdown_event.set)

    tasks = [asyncio.create_task(_schedule_entry(e)) for e in entries]

    await _shutdown_event.wait()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


# ── Entry point ──────────────────────────────────────────────────

def start_daemon(
    entries: list[dict],
    port: int = 8080,
    no_server: bool = False,
    log_format: str = "text",
    log_level: str = "INFO",
) -> None:
    """Start the daemon with scheduler + optional HTTP server."""
    global _history, _entries

    from oikb.logging import configure_logging
    configure_logging(log_format=log_format, log_level=log_level)

    _entries = entries
    _history = SyncHistory()
    set_build_info(__version__)

    # Mount Prometheus metrics endpoint.
    from prometheus_client import make_asgi_app
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    # Mount webhook routes.
    from oikb.webhooks import router as webhook_router, configure as configure_webhooks
    app.include_router(webhook_router)

    secrets: dict[str, str] = {}
    for entry in entries:
        for key in ("github_secret", "gitlab_secret", "slack_signing_secret"):
            if key in entry:
                secrets[key] = str(entry[key])
    configure_webhooks(entries=entries, run_entry=_run_entry, secrets=secrets)

    webhook_entries = [e for e in entries if e.get("webhook")]

    if no_server:
        asyncio.run(_run_scheduler(entries))
    else:
        @app.on_event("startup")
        async def _startup():
            asyncio.create_task(_run_scheduler(entries))

        click.echo(f"oikb daemon listening on port {port}")
        click.echo(f"  {len(entries)} source(s) configured")
        click.echo(f"  Auth: {'OIKB_API_KEY set' if API_KEY else 'disabled (set OIKB_API_KEY to enable)'}")
        if webhook_entries:
            click.echo(f"  {len(webhook_entries)} webhook-enabled source(s)")
        click.echo(f"  GET  http://localhost:{port}/health")
        click.echo(f"  GET  http://localhost:{port}/metrics")
        click.echo(f"  GET  http://localhost:{port}/history")
        click.echo(f"  POST http://localhost:{port}/sync/{{kb_id}}")
        if webhook_entries:
            click.echo(f"  POST http://localhost:{port}/webhooks/github|gitlab|slack|confluence")
        click.echo(f"\n  OpenAPI spec: http://localhost:{port}/openapi.json")
        click.echo(f"  Add as Tool Server in Open WebUI → Settings → Connections")

        uvicorn.run(
            app,
            host="0.0.0.0",
            port=port,
            log_level="warning",
        )
