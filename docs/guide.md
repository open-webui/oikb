# oikb Guide

A complete guide to syncing content into Open WebUI Knowledge Bases.

---

## Table of Contents

- [Installation](#installation)
- [Getting Started](#getting-started)
  - [Your First Sync](#your-first-sync)
  - [Watch Mode](#watch-mode)
- [Configuration File](#configuration-file)
  - [Generating with oikb init](#generating-with-oikb-init)
  - [Manual Setup](#manual-setup)
  - [Global Defaults](#global-defaults)
  - [Environment Variable Interpolation](#environment-variable-interpolation)
- [Sources](#sources)
  - [Local Directories](#local-directories)
  - [GitHub](#github)
  - [GitLab / Bitbucket](#gitlab--bitbucket)
  - [Confluence](#confluence)
  - [Cloud Storage (S3 / GCS / Azure)](#cloud-storage-s3--gcs--azure)
  - [SharePoint](#sharepoint)
  - [Nextcloud](#nextcloud)
  - [All Connectors](#all-connectors)
- [Filtering](#filtering)
  - [Include / Exclude Globs](#include--exclude-globs)
  - [Max File Size](#max-file-size)
  - [Splitting Sources Across KBs](#splitting-sources-across-kbs)
- [Daemon Mode](#daemon-mode)
  - [Starting the Daemon](#starting-the-daemon)
  - [Scheduling](#scheduling)
  - [API Endpoints](#api-endpoints)
  - [Authentication](#authentication)
- [Webhooks](#webhooks)
- [Enterprise Features](#enterprise-features)
  - [Prometheus Metrics](#prometheus-metrics)
  - [Structured JSON Logging](#structured-json-logging)
  - [Failure Notifications](#failure-notifications)
  - [Sync Locking](#sync-locking)
  - [Dry-Run via API](#dry-run-via-api)
- [Deployment](#deployment)
  - [Docker](#docker)
  - [Docker Compose](#docker-compose)
  - [GitHub Actions](#github-actions)
  - [Kubernetes](#kubernetes)
- [CLI Reference](#cli-reference)
- [Troubleshooting](#troubleshooting)

---

## Installation

```bash
pip install oikb
```

For specific connectors with extra dependencies:

```bash
pip install oikb[gdrive]     # Google Drive
pip install oikb[s3]         # AWS S3
pip install oikb[confluence] # Confluence
pip install oikb[all]        # Everything
```

Requires **Open WebUI 0.9.6+** and Python 3.10+.

---

## Getting Started

### Your First Sync

Set your Open WebUI credentials:

```bash
export OPEN_WEBUI_URL=http://localhost:3000
export OPEN_WEBUI_API_KEY=sk-your-api-key
```

Or save them permanently:

```bash
oikb config set url http://localhost:3000
oikb config set token sk-your-api-key
```

Find your Knowledge Base ID in Open WebUI → Knowledge → click a KB → copy the ID from the URL.

Sync a local directory:

```bash
oikb sync ./docs --kb-id your-kb-id
```

That's it. Only new and modified files are uploaded (SHA-256 diffing). Run it again and nothing happens — it's fully incremental.

### Preview Before Syncing

Always safe to preview first:

```bash
oikb sync ./docs --kb-id your-kb-id --dry-run
```

This shows exactly what would be added, modified, or deleted without touching the KB.

### Watch Mode

Auto-sync when files change:

```bash
oikb watch ./docs --kb-id your-kb-id
```

Uses filesystem events (not polling) so changes are picked up instantly.

---

## Configuration File

For multi-source setups or daemon mode, use a `.oikb.yaml` config file.

### Generating with oikb init

The fastest way to create one:

```bash
oikb init
```

This walks you through each source interactively and writes the file.

### Manual Setup

Create `.oikb.yaml` in your project root:

```yaml
sources:
  - name: docs
    source: ./docs
    kb-id: your-kb-id

  - name: wiki
    source: github:myorg/wiki
    kb-id: another-kb-id
    interval: 1h
```

Then sync all sources at once:

```bash
oikb sync
```

### Global Defaults

Avoid repeating the same config across entries:

```yaml
defaults:
  interval: 1h
  concurrency: 4
  filter:
    max-size: 50mb
  notify:
    url: https://hooks.slack.com/services/T.../B.../xxx
    on: error

sources:
  - name: docs
    source: github:myorg/docs
    kb-id: abc123

  - name: handbook
    source: confluence:ENG
    kb-id: def456
    interval: "0 6 * * 1-5"  # overrides default
```

Per-entry values override defaults. Nested dicts (filter, notify) are deep-merged.

### Environment Variable Interpolation

All string values support `${VAR}` and `${VAR:-default}`:

```yaml
sources:
  - name: docs
    source: github:${GITHUB_ORG}/docs
    kb-id: ${KB_DOCS_ID}
    token: ${GITHUB_TOKEN}
    notify:
      url: ${SLACK_WEBHOOK:-https://hooks.slack.com/fallback}
```

This enables GitOps workflows where `.oikb.yaml` is committed to the repo but secrets come from the runtime environment.

---

## Sources

### Local Directories

```bash
oikb sync ./path/to/docs --kb-id your-kb-id
```

Scans recursively. Supports any file type.

### GitHub

```bash
oikb sync github:owner/repo --kb-id your-kb-id
```

Requires `GITHUB_TOKEN` for private repos. Syncs the default branch. To specify a branch or subdirectory:

```bash
oikb sync github:owner/repo --branch develop --path docs/
```

Or in `.oikb.yaml`:

```yaml
sources:
  - name: api-docs
    source: github:myorg/api
    kb-id: abc123
    branch: main
    path: docs/
```

### GitLab / Bitbucket

```bash
oikb sync gitlab:owner/repo --kb-id your-kb-id
oikb sync bitbucket:owner/repo --kb-id your-kb-id
```

Requires `GITLAB_TOKEN` or `BITBUCKET_TOKEN` respectively.

### Confluence

```bash
oikb sync confluence:SPACE_KEY --kb-id your-kb-id
```

Requires `CONFLUENCE_URL`, `CONFLUENCE_USERNAME`, and `CONFLUENCE_API_TOKEN`.

### Cloud Storage (S3 / GCS / Azure)

```bash
oikb sync s3://bucket/prefix --kb-id your-kb-id
oikb sync gcs://bucket/prefix --kb-id your-kb-id
oikb sync azure://container/prefix --kb-id your-kb-id
```

Uses standard cloud SDK credentials (AWS profiles, service accounts, etc.).

### SharePoint

Sync a SharePoint document library:

```bash
oikb sync sharepoint:mysite.sharepoint.com --kb-id your-kb-id
oikb sync sharepoint:mysite.sharepoint.com/Documents --kb-id your-kb-id
```

SharePoint supports two authentication methods:

#### Client Secret (simpler setup)

Set these environment variables:

```bash
export SHAREPOINT_TENANT_ID=your-tenant-id
export SHAREPOINT_CLIENT_ID=your-client-id
export SHAREPOINT_CLIENT_SECRET=your-client-secret
```

#### Certificate Auth (recommended for production)

Certificate-based auth is more secure and follows Microsoft best practices for production environments.

First install the required extras:

```bash
pip install oikb[sharepoint-cert]
```

Then set:

```bash
export SHAREPOINT_TENANT_ID=your-tenant-id
export SHAREPOINT_CLIENT_ID=your-client-id
export SHAREPOINT_CERTIFICATE_PATH=/path/to/cert.pem
# Optional — only needed if the PEM key is encrypted:
export SHAREPOINT_CERTIFICATE_PASSWORD=your-password
```

The PEM file must contain both the private key and the certificate. The connector extracts the thumbprint and signs a JWT assertion automatically.

> **Note:** `SHAREPOINT_CLIENT_SECRET` and `SHAREPOINT_CERTIFICATE_PATH` are mutually exclusive — set one or the other.

#### .oikb.yaml example

```yaml
sources:
  - name: legal-docs
    source: sharepoint:contoso.sharepoint.com/Legal
    kb-id: abc123
    interval: 1h
```

### Nextcloud

Sync a Nextcloud folder via WebDAV:

```bash
export NEXTCLOUD_URL=https://nextcloud.example.com
export NEXTCLOUD_USER=svc_docs
export NEXTCLOUD_PASSWORD=your-app-password

oikb sync "nextcloud:/Documents" --kb-id your-kb-id
oikb sync "nextcloud:/Team/Engineering Handbook" --kb-id your-kb-id
```

Use a Nextcloud app password when possible. The connector resolves the
authenticated DAV user ID through the Nextcloud OCS user endpoint, so it works
with both local users and LDAP-backed accounts.

#### .oikb.yaml example

```yaml
sources:
  - name: team-docs
    source: "nextcloud:/Documents"
    kb-id: abc123
    interval: 1h
```

### All Connectors

45 connectors available. See the full list:

| Category | Sources |
|---|---|
| **Git** | GitHub, GitLab, Bitbucket |
| **Cloud Storage** | S3, GCS, Azure Blob, Dropbox, R2, Google Drive, SharePoint, Nextcloud, Egnyte, Oracle Cloud |
| **Wikis & KBs** | Confluence, Notion, BookStack, Discourse, GitBook, Guru, Outline, Slab, Document360, DokuWiki, Google Sites |
| **Ticketing** | Jira, Linear, Zendesk, Freshdesk, Asana, ClickUp, Airtable, ServiceNow, ProductBoard |
| **Messaging** | Slack, Discord, Microsoft Teams, Gmail, Zulip |
| **Meetings** | Gong, Fireflies |
| **Forums** | XenForo |
| **Sales & CRM** | Salesforce, HubSpot |
| **Web** | Website / Sitemap crawler |

---

## Filtering

### Include / Exclude Globs

Control which files get synced:

```yaml
sources:
  - name: docs
    source: github:owner/repo
    kb-id: abc123
    filter:
      include: ["docs/**/*.md", "*.txt"]
      exclude: ["drafts/**", "**/*.tmp"]
```

- `include` — only sync files matching these patterns
- `exclude` — skip files matching these patterns (applied after include)

### Max File Size

Skip files above a size limit:

```yaml
filter:
  max-size: 50mb
```

Accepts `b`, `kb`, `mb`, `gb`. Files exceeding the limit are warned and skipped:

```
⚠ Skipping model.bin (2.1 GB) — exceeds max-size (50.0 MB)
```

Also available as CLI flag: `oikb sync ./docs --kb-id abc --max-file-size 50mb`

### Splitting Sources Across KBs

Route different parts of a repo to different Knowledge Bases:

```yaml
sources:
  - name: user-docs
    source: github:owner/repo
    kb-id: abc123
    filter:
      include: ["docs/**"]

  - name: api-reference
    source: github:owner/repo
    kb-id: def456
    filter:
      include: ["api/**"]
```

---

## Daemon Mode

For production deployments, run oikb as a long-lived daemon with scheduled sync and an HTTP API.

### Starting the Daemon

```bash
oikb daemon --port 8080
```

Reads `.oikb.yaml` and syncs each source on its configured interval. Specify a config file:

```bash
oikb daemon --config /etc/oikb/config.yaml
```

### Scheduling

Simple intervals:

```yaml
interval: 30m    # every 30 minutes
interval: 1h     # every hour
interval: 6h     # every 6 hours
```

Cron expressions:

```yaml
interval: "0 6 * * 1-5"    # weekdays at 6am
interval: "0 */6 * * *"    # every 6 hours
interval: "0 0 * * 0"      # weekly on Sunday midnight
```

Both can be mixed in the same config. Auto-detected by field count.

### API Endpoints

| Endpoint | Description |
|---|---|
| `GET /health` | Sync status for all sources (k8s readiness probe) |
| `GET /health/ready` | Liveness probe |
| `GET /metrics` | Prometheus metrics |
| `GET /history` | Sync history (filterable by KB, errors) |
| `POST /sync/{name-or-kb-id}` | Trigger immediate sync |
| `POST /sync/{id}?dry_run=true` | Preview changes without uploading |
| `POST /webhooks/github` | GitHub push webhook |
| `POST /webhooks/gitlab` | GitLab push webhook |
| `POST /webhooks/slack` | Slack event webhook |
| `POST /webhooks/confluence` | Confluence update webhook |

### Authentication

Secure endpoints with an API key:

```bash
export OIKB_API_KEY=your-secret-key
oikb daemon
```

All API requests require `Authorization: Bearer your-secret-key`. Docker secrets supported via `OIKB_API_KEY_FILE`.

---

## Webhooks

Enable instant sync on push:

```yaml
sources:
  - name: docs
    source: github:owner/repo
    kb-id: abc123
    webhook: true
    github_secret: your-webhook-secret
```

Then add a webhook in GitHub → Settings → Webhooks pointing to `http://your-daemon:8080/webhooks/github`.

Supported: GitHub, GitLab, Slack, Confluence.

---

## Enterprise Features

### Prometheus Metrics

`GET /metrics` exports:

| Metric | Type | Description |
|---|---|---|
| `oikb_sync_total` | Counter | Syncs by source and status |
| `oikb_sync_duration_seconds` | Histogram | Sync duration |
| `oikb_files_uploaded_total` | Counter | Files added + modified |
| `oikb_files_deleted_total` | Counter | Files deleted |
| `oikb_sync_errors_total` | Counter | Failed syncs |
| `oikb_info` | Gauge | Build version |

Add to your Prometheus config:

```yaml
scrape_configs:
  - job_name: oikb
    static_configs:
      - targets: ["oikb:8080"]
```

### Structured JSON Logging

Enable for log aggregators (Datadog, Splunk, ELK, CloudWatch):

```bash
oikb daemon --log-format json
# or
LOG_FORMAT=json oikb daemon
```

Output (one JSON object per line):

```json
{"ts":"2025-05-21T06:00:01Z","level":"INFO","logger":"oikb.daemon","msg":"Synced github:owner/repo → abc123: 3 added (1823ms)"}
```

### Failure Notifications

POST to a webhook on sync error:

```yaml
sources:
  - name: docs
    source: github:owner/repo
    kb-id: abc123
    notify:
      url: https://hooks.slack.com/services/T.../B.../xxx
      on: error     # error (default) | always
```

The payload includes a `text` field for native Slack compatibility, plus structured fields (`source`, `status`, `error`, `duration_ms`).

### Sync Locking

Prevents overlapping syncs to the same KB. If a webhook fires while a scheduled sync is running, the duplicate is skipped:

```
INFO  Skipping github:owner/repo — sync already running for abc123
```

Automatic. No configuration needed.

### Dry-Run via API

Preview changes without uploading:

```bash
curl -X POST http://localhost:8080/sync/docs?dry_run=true \
  -H "Authorization: Bearer $OIKB_API_KEY"
```

Returns:

```json
{"dry_run": true, "result": {"added": 3, "modified": 1, "deleted": 0}}
```

---

## Deployment

### Docker

```bash
docker run -d \
  -e OPEN_WEBUI_URL=http://open-webui:8080 \
  -e OPEN_WEBUI_API_KEY=sk-... \
  -e OIKB_API_KEY=your-daemon-key \
  -e LOG_FORMAT=json \
  -v ./.oikb.yaml:/app/.oikb.yaml:ro \
  -p 8080:8080 \
  ghcr.io/open-webui/oikb:latest daemon
```

### Docker Compose

```yaml
services:
  open-webui:
    image: ghcr.io/open-webui/open-webui:main
    ports:
      - "3000:8080"

  oikb:
    image: ghcr.io/open-webui/oikb:latest
    environment:
      - OPEN_WEBUI_URL=http://open-webui:8080
      - OPEN_WEBUI_API_KEY=${OPEN_WEBUI_API_KEY}
      - OIKB_API_KEY=${OIKB_API_KEY}
      - LOG_FORMAT=json
    volumes:
      - ./.oikb.yaml:/app/.oikb.yaml:ro
    command: daemon
    ports:
      - "8080:8080"
    depends_on:
      - open-webui
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health/ready"]
      interval: 30s
      timeout: 5s
```

### GitHub Actions

```yaml
- name: Sync docs to Open WebUI
  uses: docker://ghcr.io/open-webui/oikb:latest
  with:
    args: sync /github/workspace/docs --kb-id ${{ secrets.KB_ID }}
  env:
    OPEN_WEBUI_URL: ${{ secrets.OPEN_WEBUI_URL }}
    OPEN_WEBUI_API_KEY: ${{ secrets.OPEN_WEBUI_API_KEY }}
```

### Kubernetes

Minimal deployment:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: oikb
spec:
  replicas: 1
  selector:
    matchLabels:
      app: oikb
  template:
    metadata:
      labels:
        app: oikb
    spec:
      containers:
        - name: oikb
          image: ghcr.io/open-webui/oikb:latest
          args: ["daemon"]
          ports:
            - containerPort: 8080
          env:
            - name: OPEN_WEBUI_URL
              value: http://open-webui:8080
            - name: OPEN_WEBUI_API_KEY
              valueFrom:
                secretKeyRef:
                  name: oikb-secrets
                  key: open-webui-api-key
            - name: OIKB_API_KEY
              valueFrom:
                secretKeyRef:
                  name: oikb-secrets
                  key: oikb-api-key
            - name: LOG_FORMAT
              value: json
          volumeMounts:
            - name: config
              mountPath: /app/.oikb.yaml
              subPath: .oikb.yaml
          livenessProbe:
            httpGet:
              path: /health/ready
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 30
          readinessProbe:
            httpGet:
              path: /health
              port: 8080
            initialDelaySeconds: 10
            periodSeconds: 60
      volumes:
        - name: config
          configMap:
            name: oikb-config
---
apiVersion: v1
kind: Service
metadata:
  name: oikb
spec:
  selector:
    app: oikb
  ports:
    - port: 8080
```

---

## CLI Reference

```
oikb init                           Generate .oikb.yaml interactively
oikb sync [SOURCE]                  Incremental sync
oikb sync --dry-run                 Preview without uploading
oikb sync --max-file-size 50mb      Skip large files
oikb sync --concurrency 4           Parallel uploads
oikb sync --scan-secrets            Block files with credentials
oikb watch <dir> --kb-id ID         Auto-sync on file change
oikb daemon                         Start scheduled daemon
oikb daemon --log-format json       JSON logging
oikb daemon --config /path/to/yaml  Custom config path
oikb diff <source> --kb-id ID       Preview changes
oikb validate                       Check .oikb.yaml syntax
oikb validate --deep                Verify API, auth, and KB existence
oikb ls --kb-id ID                  List files in a KB
oikb status --kb-id ID              Show KB info
oikb history                        View sync history
oikb history --json                 JSON output
oikb history --errors               Failed syncs only
oikb reset --kb-id ID               Delete all files in a KB
oikb config set url <url>           Save Open WebUI URL
oikb config set token <token>       Save API key
oikb config show                    Show saved config
```

---

## Troubleshooting

### "Connection refused" or "401 Unauthorized"

Check your credentials:

```bash
oikb config show
# or verify env vars:
echo $OPEN_WEBUI_URL
echo $OPEN_WEBUI_API_KEY
```

### "No sync entries found"

You're running `oikb sync` without arguments and there's no `.oikb.yaml` in the current directory. Either:
- Specify the source: `oikb sync ./docs --kb-id your-kb-id`
- Create a config: `oikb init`

### Large syncs are slow

- Enable concurrent uploads: `--concurrency 4` or `concurrency: 4` in yaml
- Set `filter.max-size: 50mb` to skip large binaries
- Use `filter.exclude` to skip unnecessary files

### Daemon won't start

```bash
oikb validate        # Check config syntax
oikb validate --deep # Verify API + KB connectivity
```

### How do I find my KB ID?

Open WebUI → Knowledge → click a KB → the ID is in the URL:
`http://localhost:3000/knowledge/8f3a2b1c-1234-5678-9abc-def012345678`
