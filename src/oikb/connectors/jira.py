"""Jira connector — sync issues from a Jira project to a Knowledge Base.

Auth via JIRA_URL, JIRA_USER, JIRA_TOKEN env vars.

Configurable:
  - project_key: Jira project key
  - jql: custom JQL query (overrides default project filter)
  - fields: which fields to export (default: summary, description, status, issuetype)
  - format: output format — 'markdown' (default) or 'json'
  - limit: max issues to fetch (default: unlimited)
"""

from __future__ import annotations

import hashlib
import json as json_mod
import os
from typing import Any

import httpx

from oikb.connectors import BaseConnector, ManifestEntry


class JiraConnector(BaseConnector):
    """Sync issues from a Jira project as text files."""

    def __init__(
        self,
        project_key: str,
        jql: str | None = None,
        fields: list[str] | None = None,
        fmt: str = "markdown",
        limit: int | None = None,
        base_url: str | None = None,
        user: str | None = None,
        token: str | None = None,
    ):
        self.project_key = project_key
        self._base_url = (base_url or os.environ.get("JIRA_URL", "")).rstrip("/")
        self._user = user or os.environ.get("JIRA_USER", "")
        self._token = token or os.environ.get("JIRA_TOKEN", "")

        if not self._base_url:
            raise ValueError("Jira URL required. Set JIRA_URL env var.")
        if not self._token:
            raise ValueError("Jira API token required. Set JIRA_TOKEN env var.")

        self._jql = jql or f"project={self.project_key}"
        self._fields = fields or ["summary", "description", "status", "issuetype", "priority", "assignee"]
        self._fmt = fmt
        self._limit = limit
        self._http = httpx.Client(
            base_url=self._base_url,
            auth=(self._user, self._token) if self._user else None,
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        self._issue_cache: dict[str, str] = {}

    def build_manifest(self) -> list[ManifestEntry]:
        entries: list[ManifestEntry] = []
        api_fields = list(set(self._fields + ["summary", "updated"]))
        next_page_token = None
        fetched = 0

        while True:
            max_results = 100

            if self._limit:
                max_results = min(100, self._limit - fetched)

            if max_results <= 0:
                break

            params = {
                "jql": self._jql,
                "maxResults": max_results,
                "fields": ",".join(api_fields),
            }

            if next_page_token:
                params["nextPageToken"] = next_page_token

            resp = self._http.get(
                "/rest/api/3/search/jql",
                params=params,
            )
            resp.raise_for_status()
            data = resp.json()
            issues = data.get("issues", [])

            for issue in issues:
                key = issue["key"]
                fields = issue["fields"]
                updated = fields.get("updated", "")
                checksum = hashlib.sha256(f"{key}:{updated}".encode()).hexdigest()[:16]
                text = self._format_issue(issue)
                ext = ".json" if self._fmt == "json" else ".md"
                filename = f"{key}{ext}"
                self._issue_cache[filename] = text
                entries.append(ManifestEntry(filename=filename, path="", checksum=checksum, size=len(text.encode())))

            fetched += len(issues)

            next_page_token = data.get("nextPageToken")

            if not issues or not next_page_token:
                break

        entries.sort(key=lambda e: e.display_path)
        return entries

    def _resolve_field(self, fields: dict, field_name: str) -> str:
        """Extract display value from a Jira field."""
        val = fields.get(field_name)
        if val is None:
            return ""
        if isinstance(val, dict):
            # ADF document (description)
            if val.get("type") == "doc":
                return self._adf_to_text(val)
            # Object with name (status, issuetype, priority, assignee)
            return val.get("name", val.get("displayName", val.get("value", str(val))))
        if isinstance(val, list):
            return ", ".join(str(v.get("name", v) if isinstance(v, dict) else v) for v in val)
        return str(val)

    def _format_issue(self, issue: dict) -> str:
        fields = issue["fields"]
        key = issue["key"]

        if self._fmt == "json":
            data = {"key": key}
            for f in self._fields:
                data[f] = self._resolve_field(fields, f)
            return json_mod.dumps(data, indent=2, ensure_ascii=False)

        # Default: markdown
        summary = fields.get("summary", "")
        lines = [f"# [{key}] {summary}", ""]
        for f in self._fields:
            if f in ("summary", "description"):
                continue
            val = self._resolve_field(fields, f)
            if val:
                lines.append(f"**{f}**: {val}")
        desc = self._resolve_field(fields, "description") if "description" in self._fields else ""
        if desc:
            lines.extend(["", desc])
        return "\n".join(lines)

    def _adf_to_text(self, doc: dict | None) -> str:
        if not doc:
            return ""
        parts: list[str] = []
        for node in doc.get("content", []):
            if node.get("type") == "paragraph":
                text = "".join(c.get("text", "") for c in node.get("content", []) if c.get("type") == "text")
                parts.append(text)
            elif node.get("type") == "heading":
                text = "".join(c.get("text", "") for c in node.get("content", []) if c.get("type") == "text")
                level = node.get("attrs", {}).get("level", 2)
                parts.append(f"{'#' * level} {text}")
            elif node.get("type") == "codeBlock":
                text = "".join(c.get("text", "") for c in node.get("content", []) if c.get("type") == "text")
                parts.append(f"```\n{text}\n```")
        return "\n".join(parts)

    def read_file(self, path: str, filename: str) -> bytes:
        text = self._issue_cache.get(filename)
        if not text:
            raise FileNotFoundError(f"Issue not found: {filename}")
        return text.encode("utf-8")

    def close(self) -> None:
        self._http.close()


def parse_jira_source(source: str) -> dict[str, str | None]:
    """Parse jira:<PROJECT_KEY> or jira:<PROJECT_KEY>?jql=...&fields=...&format=..."""
    raw = source.removeprefix("jira:")
    parts = raw.split("?", 1)
    project_key = parts[0]
    if not project_key:
        raise ValueError("Invalid Jira source. Expected: jira:<PROJECT_KEY>")
    result: dict[str, str | None] = {"project_key": project_key}
    if len(parts) > 1:
        for param in parts[1].split("&"):
            if "=" in param:
                k, v = param.split("=", 1)
                result[k] = v
    return result
