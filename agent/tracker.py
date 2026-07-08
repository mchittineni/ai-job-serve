import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT PRIMARY KEY,
    title       TEXT,
    company     TEXT,
    url         TEXT,
    source      TEXT,
    rate        TEXT,
    status      TEXT,      -- skipped | evaluated | draft | applied | error
    score       INTEGER,
    reason      TEXT,
    first_seen  TEXT,
    updated_at  TEXT,
    payload     TEXT
);
"""


class Tracker:
    """SQLite-backed state so re-runs never re-process (or re-apply to) the same job."""

    def __init__(self, data_dir: Path):
        self.db_path = data_dir / "jobs.db"
        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute(SCHEMA)
        self.conn.commit()

    def seen(self, job_id: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return row is not None

    def record(
        self, job: Job, status: str, score: int | None = None, reason: str = "", payload: dict | None = None
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.conn.execute(
            """INSERT INTO jobs (id, title, company, url, source, rate, status, score,
                                 reason, first_seen, updated_at, payload)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 status = excluded.status, score = excluded.score,
                 reason = excluded.reason, updated_at = excluded.updated_at,
                 payload = excluded.payload""",
            (
                job.id,
                job.title,
                job.company,
                job.url,
                job.source,
                job.rate,
                status,
                score,
                reason,
                now,
                now,
                json.dumps(payload or {}),
            ),
        )
        self.conn.commit()

    def applications_today(self) -> int:
        today = datetime.now(UTC).date().isoformat()
        row = self.conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE status = 'applied' AND updated_at >= ?",
            (today,),
        ).fetchone()
        return row[0] if row else 0

    def stats(self) -> dict:
        rows = self.conn.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        return dict(rows)

    def close(self) -> None:
        self.conn.close()
