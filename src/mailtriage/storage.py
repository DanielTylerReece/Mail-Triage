"""SQLite-backed audit / state storage."""

from __future__ import annotations

import contextlib
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)


@dataclass
class Decision:
    timestamp: str
    mailbox: str
    message_id: str
    sender: str | None
    subject: str | None
    source: str          # 'tier1' | 'tier2' | 'guard' | 'error' | 'dedup'
    rule: str | None
    category: str
    confidence: float | None
    reasoning: str | None
    action: str          # 'moved' | 'kept' | 'aborted' | 'aborted-permanent'
                          # | 'skipped-duplicate' | 'dry-run'
    destination: str | None
    provider: str | None
    model: str | None
    error: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT NOT NULL,
    mailbox         TEXT NOT NULL,
    message_id      TEXT NOT NULL,
    sender          TEXT,
    subject         TEXT,
    source          TEXT NOT NULL,
    rule            TEXT,
    category        TEXT NOT NULL,
    confidence      REAL,
    reasoning       TEXT,
    action          TEXT,
    destination     TEXT,
    provider        TEXT,
    model           TEXT,
    error           TEXT
);
CREATE INDEX IF NOT EXISTS idx_decisions_mailbox_ts ON decisions(mailbox, timestamp);
CREATE INDEX IF NOT EXISTS idx_decisions_message    ON decisions(mailbox, message_id);
"""


# Actions that count as "this message has been seen and resolved." A future
# webhook redelivery for the same message_id will be marked
# `skipped-duplicate` instead of re-running the whole pipeline.
#
# `dry-run` IS in this set: if you ran in DRY_RUN mode for a week and then
# turned it off, mail that was already dry-run-classified stays in the
# Inbox and is NOT re-processed when the same notification re-delivers.
# (In practice Graph won't re-deliver a stale notification, but the dedup
# is belt-and-braces.)
PROCESSED_ACTIONS = frozenset({
    "moved", "kept", "skipped-duplicate", "aborted-permanent", "dry-run",
})


class SqliteStorage:
    """Single-host SQLite-backed storage. WAL mode + busy_timeout makes this
    safe for the single-process / multi-worker model."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # Verify writability early — prevents confusing SQLite errors on
        # the first record() call.
        probe = db_path.parent / ".write_test"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            raise RuntimeError(f"data dir {db_path.parent} not writable: {e}") from e

        self._lock = threading.Lock()
        with contextlib.closing(self._conn()) as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.db_path, isolation_level=None, timeout=10.0)
        c.row_factory = sqlite3.Row
        return c

    def record(self, d: Decision) -> None:
        # contextlib.closing ensures the connection is closed on every path.
        # The plain sqlite3 connection context manager only commits/rolls
        # back — it does NOT close — which leaks a file descriptor per call.
        with self._lock, contextlib.closing(self._conn()) as c:
            c.execute(
                """INSERT INTO decisions
                   (timestamp, mailbox, message_id, sender, subject, source, rule,
                    category, confidence, reasoning, action, destination,
                    provider, model, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    d.timestamp, d.mailbox, d.message_id, d.sender, d.subject,
                    d.source, d.rule, d.category, d.confidence, d.reasoning,
                    d.action, d.destination, d.provider, d.model, d.error,
                ),
            )

    def has_processed(self, mailbox: str, message_id: str) -> bool:
        # Match the canonical processed-actions set. Generic transient
        # `aborted` rows do NOT count, so the message can be retried on
        # a later webhook delivery.
        placeholders = ",".join("?" for _ in PROCESSED_ACTIONS)
        with contextlib.closing(self._conn()) as c:
            row = c.execute(
                f"""SELECT 1 FROM decisions
                    WHERE mailbox = ? AND message_id = ?
                      AND action IN ({placeholders})
                    LIMIT 1""",
                (mailbox, message_id, *PROCESSED_ACTIONS),
            ).fetchone()
        return row is not None

    def recent(self, mailbox: str, since_iso: str) -> Iterable[Decision]:
        with contextlib.closing(self._conn()) as c:
            rows = c.execute(
                """SELECT * FROM decisions
                   WHERE mailbox = ? AND timestamp >= ?
                   ORDER BY timestamp ASC""",
                (mailbox, since_iso),
            ).fetchall()
        for r in rows:
            yield Decision(
                timestamp=r["timestamp"], mailbox=r["mailbox"],
                message_id=r["message_id"], sender=r["sender"],
                subject=r["subject"], source=r["source"], rule=r["rule"],
                category=r["category"], confidence=r["confidence"],
                reasoning=r["reasoning"], action=r["action"],
                destination=r["destination"], provider=r["provider"],
                model=r["model"], error=r["error"],
            )

    def purge_older_than(self, before_iso: str) -> int:
        """Delete decision rows with timestamp strictly before `before_iso`.
        Returns the number of rows removed."""
        with self._lock, contextlib.closing(self._conn()) as c:
            cur = c.execute(
                "DELETE FROM decisions WHERE timestamp < ?", (before_iso,),
            )
            return cur.rowcount or 0


def now_iso() -> str:
    """Always UTC. Lexicographic ordering on the TEXT timestamp depends
    on this — never use a non-UTC offset here."""
    return datetime.now(timezone.utc).isoformat()
