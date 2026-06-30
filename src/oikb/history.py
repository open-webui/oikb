"""Sync history tracking via SQLite."""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

from oikb.config import CONFIG_DIR


_DEFAULT_DB = CONFIG_DIR / "history.db" if CONFIG_DIR.exists() else Path.home() / ".oikb" / "history.db"

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS sync_log (
    id             TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    kb_id          TEXT NOT NULL,
    status         TEXT NOT NULL,
    started_at     REAL NOT NULL,
    finished_at    REAL,
    duration_ms    INTEGER,
    files_added    INTEGER DEFAULT 0,
    files_modified INTEGER DEFAULT 0,
    files_deleted  INTEGER DEFAULT 0,
    unmodified     INTEGER DEFAULT 0,
    error_message  TEXT,
    created_at     REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sync_log_kb_id  ON sync_log(kb_id);
CREATE INDEX IF NOT EXISTS idx_sync_log_source ON sync_log(source);
CREATE INDEX IF NOT EXISTS idx_sync_log_status ON sync_log(status);
"""


class SyncHistory:
    """Lightweight sync history backed by a local SQLite database."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or _DEFAULT_DB
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn: sqlite3.Connection | None = None

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.executescript(_SCHEMA)
        return self._conn

    def log(
        self,
        source: str,
        kb_id: str,
        status: str,
        started_at: float,
        files_added: int = 0,
        files_modified: int = 0,
        files_deleted: int = 0,
        unmodified: int = 0,
        error: str | None = None,
    ) -> None:
        """Record a sync result."""
        now = time.time()
        duration_ms = int((now - started_at) * 1000)
        conn = self._get_conn()
        conn.execute(
            """INSERT INTO sync_log
               (id, source, kb_id, status, started_at, finished_at,
                duration_ms, files_added, files_modified, files_deleted,
                unmodified, error_message, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(uuid.uuid4()),
                source,
                kb_id,
                status,
                started_at,
                now,
                duration_ms,
                files_added,
                files_modified,
                files_deleted,
                unmodified,
                error,
                now,
            ),
        )
        conn.commit()

    def query(
        self,
        limit: int = 20,
        kb_id: str | None = None,
        errors_only: bool = False,
    ) -> list[dict[str, Any]]:
        """Retrieve recent sync log entries."""
        conn = self._get_conn()
        sql = "SELECT * FROM sync_log WHERE 1=1"
        params: list[Any] = []

        if kb_id:
            sql += " AND kb_id = ?"
            params.append(kb_id)
        if errors_only:
            sql += " AND status = 'error'"

        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(limit)

        rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def last_sync(self, source: str) -> dict[str, Any] | None:
        """Get the most recent sync entry for a source."""
        conn = self._get_conn()
        row = conn.execute(
            "SELECT * FROM sync_log WHERE source = ? ORDER BY started_at DESC LIMIT 1",
            (source,),
        ).fetchone()
        return dict(row) if row else None

    def clear(self, older_than_days: int = 30) -> int:
        """Prune entries older than N days. Returns count deleted."""
        conn = self._get_conn()
        cutoff = time.time() - (older_than_days * 86400)
        cursor = conn.execute(
            "DELETE FROM sync_log WHERE created_at < ?", (cutoff,)
        )
        conn.commit()
        return cursor.rowcount

    def close(self) -> None:
        if self._conn:
            self._conn.close()
            self._conn = None
