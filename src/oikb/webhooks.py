"""Webhook routes for the oikb daemon -- real-time sync on push."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, Request, HTTPException

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", include_in_schema=False)

# Set by daemon.py on startup.
_hooks: list[dict] = []
_run_entry = None  # async callable
_secrets: dict[str, str] = {}


def configure(entries: list[dict], run_entry, secrets: dict[str, str]) -> None:
    """Called by daemon on startup to wire webhook state."""
    global _hooks, _run_entry, _secrets
    hooks = prepare_webhook_entries(entries)
    _hooks = hooks
    _run_entry = run_entry
    _secrets = secrets


def prepare_webhook_entries(entries: list[dict]) -> list[dict]:
    """Prepare one registry of enabled sources and their provider-specific metadata."""
    hooks = []
    for entry in entries:
        if not entry.get("webhook"):
            continue
        hook = {"entry": entry, "provider": str(entry.get("source", "")).partition(":")[0]}
        if hook["provider"] == "gitea":
            hook.update(_prepare_gitea_metadata(entry))
        hooks.append(hook)
    return hooks


def _prepare_gitea_metadata(entry: dict) -> dict:
    """Validate Gitea identity and signing configuration without making requests."""
    from oikb.connectors.gitea import GiteaConnector, parse_gitea_source

    secret = entry.get("gitea_secret")
    if not isinstance(secret, str) or not secret.strip() or "${" in secret:
        raise ValueError("Gitea webhook sources require a nonempty resolved gitea_secret")
    if not entry.get("kb-id"):
        raise ValueError("Gitea webhook sources require kb-id")
    parsed = parse_gitea_source(entry["source"])
    parsed["path"] = entry.get("path") if entry.get("path") is not None else parsed["path"]
    with GiteaConnector(**parsed, branch=entry.get("branch"), **entry.get("auth", {})) as connector:
        return {"secret": secret, "base_url": connector._base_url,
                "full_name": f"{connector.owner}/{connector.repo}".casefold(),
                "wiki": connector.is_wiki, "branch": connector.branch}


def _gitea_repository_matches(hook: dict, html_url: str) -> bool:
    """Compare origin, context path, and repository; never follow payload URLs."""
    try:
        actual, base = urlsplit(html_url), urlsplit(hook["base_url"])
        if actual.username is not None or actual.password is not None or actual.query or actual.fragment:
            return False
        port = lambda u: u.port or (443 if u.scheme == "https" else 80)
        if (actual.scheme, actual.hostname, port(actual)) != (base.scheme, base.hostname, port(base)):
            return False
        context, owner, repo = actual.path.rstrip("/").rsplit("/", 2)
        return (context == base.path.rstrip("/")
                and f"{unquote(owner)}/{unquote(repo)}".casefold() == hook["full_name"])
    except ValueError:
        return False


@router.post("/gitea")
async def gitea_webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Gitea-Signature", "")
    if len(signature) != 64 or not signature.isascii():
        raise HTTPException(status_code=403, detail="Invalid signature")
    hooks = [hook for hook in _hooks if hook["provider"] == "gitea"]
    verified = {
        secret for secret in {hook["secret"] for hook in hooks}
        if hmac.compare_digest(hmac.new(secret.encode(), body, hashlib.sha256).hexdigest(), signature)
    }
    if not verified:
        raise HTTPException(status_code=403, detail="Invalid signature")
    try:
        data = await request.json()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object")
    event = request.headers.get("X-Gitea-Event")
    if event not in {"push", "wiki"}:
        return {"status": "ignored"}
    repo = data.get("repository")
    if (not isinstance(repo, dict) or not isinstance(repo.get("full_name"), str)
            or not repo["full_name"] or not isinstance(repo.get("html_url"), str) or not repo["html_url"]):
        raise HTTPException(status_code=400, detail="Missing repository full_name or html_url")
    if event == "push":
        ref, after = data.get("ref"), data.get("after")
        if not isinstance(ref, str) or not ref or not isinstance(after, str) or not after:
            raise HTTPException(status_code=400, detail="Missing push ref or after")
        if not ref.startswith("refs/heads/") or set(after) == {"0"}:
            return {"status": "ignored"}
    elif not isinstance(data.get("action"), str) or data["action"] not in {"created", "edited", "deleted"}:
        raise HTTPException(status_code=400, detail="Invalid wiki action")

    matches = {}
    for hook in hooks:
        if (hook["secret"] not in verified or repo["full_name"].casefold() != hook["full_name"]
                or not _gitea_repository_matches(hook, repo["html_url"]) or hook["wiki"] != (event == "wiki")):
            continue
        if event == "push":
            branch = hook["branch"] or repo.get("default_branch")
            if not isinstance(branch, str) or not branch:
                raise HTTPException(status_code=400, detail="Missing repository default_branch")
            if ref != "refs/heads/" + branch:
                continue
        entry = hook["entry"]
        matches.setdefault(entry["kb-id"], entry)
    if not matches or not _run_entry:
        return {"status": "ignored"}
    for entry in matches.values():
        log.info("Gitea %s webhook for %s -> %s", event, entry.get("name", entry["source"]), entry["kb-id"])
        asyncio.create_task(_run_entry(entry))
    return {"status": "accepted", "scheduled": len(matches)}


def _find_entry_by_source(prefix: str, match: str) -> dict | None:
    """Find a .oikb.yaml entry matching a source and with webhook: true."""
    for hook in _hooks:
        entry = hook["entry"]
        source = entry.get("source", "")
        if source.startswith(prefix) and match in source:
            return entry
    return None


def _verify_hmac_sha256(payload: bytes, signature: str, secret: str) -> bool:
    """Validate HMAC-SHA256 signature."""
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/github")
async def github_webhook(request: Request):
    """Handle GitHub push events."""
    payload = await request.body()

    secret = _secrets.get("github_secret", "")
    if secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not _verify_hmac_sha256(payload, sig, secret):
            raise HTTPException(status_code=403, detail="Invalid signature")

    data = await request.json()
    repo = data.get("repository", {}).get("full_name", "")
    entry = _find_entry_by_source("github:", repo)
    if entry and _run_entry:
        log.info(f"Webhook trigger: github push to {repo}")
        asyncio.create_task(_run_entry(entry))
        return {"status": "accepted", "source": entry["source"]}
    return {"status": "ignored", "reason": f"No matching entry for github:{repo}"}


@router.post("/gitlab")
async def gitlab_webhook(request: Request):
    """Handle GitLab push events."""
    secret = _secrets.get("gitlab_secret", "")
    if secret:
        token = request.headers.get("X-Gitlab-Token", "")
        if not hmac.compare_digest(token, secret):
            raise HTTPException(status_code=403, detail="Invalid token")

    data = await request.json()
    project = data.get("project", {}).get("path_with_namespace", "")
    entry = _find_entry_by_source("gitlab:", project)
    if entry and _run_entry:
        log.info(f"Webhook trigger: gitlab push to {project}")
        asyncio.create_task(_run_entry(entry))
        return {"status": "accepted", "source": entry["source"]}
    return {"status": "ignored"}


@router.post("/slack")
async def slack_webhook(request: Request):
    """Handle Slack Events API."""
    data = await request.json()

    # URL verification challenge.
    if data.get("type") == "url_verification":
        return {"challenge": data.get("challenge")}

    # Validate signing secret if configured.
    secret = _secrets.get("slack_signing_secret", "")
    if secret:
        body = await request.body()
        ts = request.headers.get("X-Slack-Request-Timestamp", "")
        sig = request.headers.get("X-Slack-Signature", "")
        base = f"v0:{ts}:{body.decode()}"
        expected = "v0=" + hmac.new(secret.encode(), base.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise HTTPException(status_code=403, detail="Invalid signature")

    event = data.get("event", {})
    channel = event.get("channel", "")
    entry = _find_entry_by_source("slack:", channel)
    if entry and _run_entry:
        log.info(f"Webhook trigger: slack message in {channel}")
        asyncio.create_task(_run_entry(entry))
        return {"status": "accepted"}
    return {"status": "ignored"}


@router.post("/confluence")
async def confluence_webhook(request: Request):
    """Handle Confluence page update webhooks."""
    data = await request.json()
    space_key = data.get("page", {}).get("spaceKey", "") or data.get("space", {}).get("key", "")
    entry = _find_entry_by_source("confluence:", space_key)
    if entry and _run_entry:
        log.info(f"Webhook trigger: confluence update in {space_key}")
        asyncio.create_task(_run_entry(entry))
        return {"status": "accepted"}
    return {"status": "ignored"}
