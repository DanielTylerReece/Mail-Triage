"""Tests for audit-DB retention purge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from mailtriage.storage import Decision, SqliteStorage


def _decision(*, ts: str, mid: str = "m") -> Decision:
    return Decision(
        timestamp=ts, mailbox="alice@example.com", message_id=mid,
        sender=None, subject=None, source="tier1", rule=None,
        category="newsletter", confidence=1.0, reasoning=None,
        action="moved", destination="Newsletters",
        provider=None, model=None, error=None,
    )


def _ts(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()


def test_purge_removes_only_old_rows(tmp_path: Path) -> None:
    s = SqliteStorage(tmp_path / "audit.db")
    s.record(_decision(ts=_ts(95), mid="old1"))
    s.record(_decision(ts=_ts(91), mid="old2"))
    s.record(_decision(ts=_ts(89), mid="recent1"))
    s.record(_decision(ts=_ts(1),  mid="recent2"))

    cutoff = _ts(90)
    deleted = s.purge_older_than(cutoff)
    assert deleted == 2

    # Recent rows survive.
    assert s.has_processed("alice@example.com", "recent1")
    assert s.has_processed("alice@example.com", "recent2")
    # Old rows gone — no audit row means no dedup record either.
    assert not s.has_processed("alice@example.com", "old1")


def test_purge_with_no_matches_returns_zero(tmp_path: Path) -> None:
    s = SqliteStorage(tmp_path / "audit.db")
    s.record(_decision(ts=_ts(1)))
    deleted = s.purge_older_than(_ts(30))
    assert deleted == 0


def test_purge_on_empty_db(tmp_path: Path) -> None:
    s = SqliteStorage(tmp_path / "audit.db")
    assert s.purge_older_than(_ts(30)) == 0
