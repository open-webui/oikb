# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.3.7] - 2026-06-02

### Added

- **Gitea**: repository source connector via `gitea:owner/repo`, configurable with `GITEA_URL` and optional `GITEA_TOKEN`. Supports branch and subdirectory scoping, uses Gitea Git tree blob SHAs for incremental sync checksums, and syncs files through the raw content API.

## [0.3.6] - 2026-05-28

### Added

- **SharePoint**: certificate-based authentication via `SHAREPOINT_CERTIFICATE_PATH` env var — more secure alternative to client secret auth. Supports encrypted PEM keys with `SHAREPOINT_CERTIFICATE_PASSWORD`. Requires `pip install oikb[sharepoint-cert]`.

## [0.3.5] - 2025-05-21

### Added

- Status dashboard at `GET /` — monospace status page rendered client-side from `/health`. Shows source name, status dot, last sync time, duration, file counts, next sync. Polls every 10s. Zero dependencies.

## [0.3.4] - 2025-05-21

### Added

- `oikb validate --deep` — verifies Open WebUI connectivity, API key validity, and that each KB ID exists. Catches config errors before deployment.

### Fixed

- `docker-compose.yaml` updated from stale `watch` mode to production `daemon` mode with healthcheck, `LOG_FORMAT=json`, and `OIKB_API_KEY` support.
- `validate` now applies `defaults:` and env var interpolation to entries before checking (was only doing raw yaml).

## [0.3.3] - 2025-05-21

### Added

- `oikb init` — interactive wizard that generates `.oikb.yaml`. Prompts for source, KB ID, name, and interval. Outputs next-step commands. Reduces onboarding from "read docs and write YAML" to "answer 4 questions."

## [0.3.2] - 2025-05-21

### Added

- Global `defaults:` key in `.oikb.yaml` — set `interval`, `concurrency`, `filter`, `notify`, or any other key once and have it apply to all entries. Per-entry values override defaults. Deep merges nested dicts (filter, notify).

## [0.3.1] - 2025-05-21

### Added

- Per-KB sync locking — prevents overlapping syncs to the same Knowledge Base. If a webhook fires while a scheduled sync is running, the duplicate is skipped with a log message.
- Dry-run via API — `POST /sync/{id}?dry_run=true` previews changes without uploading, returns added/modified/deleted counts.
- Environment variable interpolation in `.oikb.yaml` — `${VAR}` and `${VAR:-default}` syntax in all string values. Enables GitOps workflows where secrets come from the runtime.

## [0.3.0] - 2025-05-21

### Added

- Webhook failure notifications — `notify` key in `.oikb.yaml` entries POSTs a JSON payload to any HTTP endpoint on sync error. Includes a `text` field for native Slack incoming webhook compatibility. Supports `on: error` (default) or `on: always`.

## [0.2.9] - 2025-05-21

### Added

- `filter.max-size` — skip files exceeding a size limit (e.g. `50mb`, `1gb`). Configurable per-entry in `.oikb.yaml` or via `--max-file-size` CLI flag. Oversized files are warned and excluded before diffing.

## [0.2.8] - 2025-05-21

### Added

- Structured JSON logging for the daemon — `oikb daemon --log-format json` or `LOG_FORMAT=json` env var. Outputs one JSON object per line with `ts`, `level`, `logger`, `msg`, and optional `source`/`kb_id`/`duration_ms` fields. Compatible with Datadog, Splunk, ELK, CloudWatch, and Loki.

## [0.2.7] - 2025-05-21

### Added

- Cron expression support for daemon scheduling — use `interval: "0 6 * * 1-5"` alongside simple intervals (`30m`, `1h`). Auto-detected, no config flag needed.
- `croniter>=2.0` added as a core dependency.

## [0.2.6] - 2025-05-21

### Added

- Prometheus metrics endpoint (`GET /metrics`) on the daemon — exports `oikb_sync_total`, `oikb_sync_duration_seconds`, `oikb_files_uploaded_total`, `oikb_files_deleted_total`, `oikb_sync_errors_total`, and `oikb_info` build metadata. All counters labeled by source.
- `prometheus-client>=0.20` added as a core dependency.

## [0.2.5] - 2025-05-21

### Added

- Rich progress bars for sync operations — spinner during scan/diff, progress bar during uploads with file count and elapsed time.
- `rich>=13.0` added as a core dependency.

## [0.2.4] - 2025-05-21

### Added

- `oikb validate` command — validate `.oikb.yaml` without running syncs.
- `--concurrency` flag for `oikb sync` — opt-in parallel uploads (also configurable per-entry in `.oikb.yaml`).
- Upload retry with exponential backoff — transient 5xx errors retry up to 3 times.

### Fixed

- Daemon now applies `filter.include`/`filter.exclude` from `.oikb.yaml` (previously silently ignored).
- Connector HTTP clients are now properly closed after sync completes.
- GitHub connector caches default branch lookup — eliminates redundant API calls per file.

### Changed

- Removed 30+ no-op optional dependencies from `pyproject.toml` (`[github]`, `[confluence]`, etc.) that just re-declared httpx (already a core dep). Only extras with real dependencies remain: `s3`, `gcs`, `azure`, `dropbox`, `r2`, `gdrive`, `gmail`, `gsites`, `web`, `oracle`.

## [0.2.3] - 2025-05-21

### Removed

- **Breaking:** `routes` key in `.oikb.yaml`. Use multiple entries with `filter.include` instead — each entry is one source → one KB.

## [0.2.2] - 2025-05-21

### Added

- `name` key for `.oikb.yaml` entries — use friendly names to target syncs via CLI (`--name wiki`) or API (`POST /sync/wiki`).

### Changed

- Daemon sync endpoint changed from `/sync/{source}` to `/sync/{identifier}` — accepts `name` or `kb-id` (UUIDs).

## [0.2.1] - 2025-05-20

### Added

- API key authentication for daemon endpoints (`OIKB_API_KEY` env var, Docker secrets `_FILE` supported).
- Configurable fields, format, and query filters for Jira and ServiceNow connectors.

### Changed

- `.oikb.yaml` top-level key renamed from `sync:` to `sources:` (backward compatible).
- `/health` endpoints remain public; `/history` and `/sync` require auth when key is set.

## [0.2.0] - 2025-05-20

### Added

- `oikb daemon` command: long-lived scheduler with FastAPI server, /health and /history endpoints.
- `oikb history` command: view sync history from local SQLite database.
- Webhook support: /webhooks/github, /webhooks/gitlab, /webhooks/slack, /webhooks/confluence for real-time sync triggers.
- `--json` output flag for `history` command.
- 13 new connectors: Document360, Slab, Outline, Google Sites, Egnyte, Oracle Cloud Storage, ProductBoard, XenForo, Zulip, Gong, Fireflies, DokuWiki, ServiceNow. Total: 44 connectors.
- Selective sync filters: `filter.include` / `filter.exclude` glob patterns to narrow sync scope.
- Daemon doubles as an OpenAPI tool server for Open WebUI (Settings → Connections → Tool Server).

### Changed

- FastAPI and uvicorn are now core dependencies.

## [0.1.3] - 2025-05-20

### Changed

- Renamed `.oikb.yaml` config key from `kb` to `kb-id` to align with the CLI flag.

## [0.1.2] - 2025-05-20

### Changed

- Messaging connectors (Slack, Discord, Teams) now split messages by day for truly incremental sync. Past days are immutable so their checksums never change.

## [0.1.1] - 2025-05-20

### Added

- **28 new connectors** bringing the total to 31, covering code repos, cloud storage, wikis, ticketing, messaging, CRM, and web:
  - **Code Repos**: GitLab (`gitlab:owner/repo`), Bitbucket (`bitbucket:owner/repo`)
  - **Cloud Storage**: Google Cloud Storage (`gs://`), Azure Blob (`az://`), Dropbox (`dropbox:`), Cloudflare R2 (`r2://`)
  - **Wikis & Knowledge Bases**: Confluence (`confluence:`), Notion (`notion:`), BookStack (`bookstack:`), Discourse (`discourse:`), GitBook (`gitbook:`), Guru (`guru:`)
  - **Ticketing & Tasks**: Jira (`jira:`), Linear (`linear:`), Zendesk (`zendesk:`), Freshdesk (`freshdesk:`), Asana (`asana:`), ClickUp (`clickup:`), Airtable (`airtable:`)
  - **Messaging**: Slack (`slack:`), Discord (`discord:`), Microsoft Teams (`teams:`), Gmail (`gmail:`)
  - **Sales & CRM**: Salesforce (`salesforce:`), HubSpot (`hubspot:`)
  - **Other**: Google Drive (`gdrive:`), SharePoint (`sharepoint:`), Web Crawler (`web:`)
- Context manager support for `OikbClient`
- `.gitignore` added to the project

### Changed

- Renamed `--kb` to `--kb-id` across all commands
- Modernized README with 30+ connector showcase organized by category
- Added Open WebUI 0.9.6+ version requirement note
- Updated author to Tim Baek
- Moved `import json` to top-level in `client.py`
- Removed unused imports from `sync.py`, `config.py`
- Fixed `Optional[str]` type hints to use `str | None` syntax
- Removed redundant `fnmatch` check in `ignore.py`
- Empty source message now respects `--quiet` flag

## [0.1.0] - 2025-05-20

### Added

- Initial release of oikb, a CLI tool for syncing content to Open WebUI Knowledge Bases.
- Incremental sync (`oikb sync`) with SHA-256 diffing. Supports local directories, GitHub repos, and S3 buckets.
- Dry-run preview (`oikb diff`).
- Watch mode (`oikb watch`) with debounced filesystem monitoring via watchdog.
- List files (`oikb ls`), status (`oikb status`), reset (`oikb reset`) commands.
- Configuration (`oikb config`) via config file, env vars, or CLI flags.
- Declarative config (`.oikb.yaml`) for multi-source sync.
- Ignore patterns (`.oikbignore`) with gitignore-style file exclusion.
- GitHub connector (`github:owner/repo`) via Trees API. No local clone needed.
- S3 connector (`s3://bucket/prefix`) via boto3 using ETags as checksums.
- Docker image with multi-arch support (amd64 + arm64).
- GitHub Actions workflows for Docker builds (GHCR) and releases.
