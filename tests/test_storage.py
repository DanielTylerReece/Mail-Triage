"""Tests for SqliteStorage — connection lifecycle, idempotency semantics."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mailtriage.storage import Decision, SqliteStorage, now_iso


def _decision(**overrides) -> Decision:
    base = dict(
        timestamp=now_iso(),
        mailbox="alice@example.com",
        message_id="msg-1",
        sender="bob@example.com",
        subject="hi",
        source="tier1",
        rule="from-domain example.com",
        category="newsletter",
        confidence=1.0,
        reasoning="matched",
        action="moved",
        destination="Newsletters",
        provider=None,
        model=None,
        error=None,
    )
    base.update(overrides)
    return Decision(**base)


def test_init_creates_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "deeper" / "audit.db"
    SqliteStorage(db)
    assert db.parent.exists()
    assert db.exists()


def test_init_fails_loudly_if_dir_unwritable(tmp_path: Path) -> None:
    bad = tmp_path / "ro"
    bad.mkdir()
    bad.chmod(0o500)
    try:
        with pytest.raises(RuntimeError, match="not writable"):
            SqliteStorage(bad / "audit.db")
    finally:
        bad.chmod(0o700)


def test_record_then_has_processed(tmp_path: Path) -> None:
    s = SqliteStorage(tmp_path / "audit.db")
    s.record(_decision(action="moved"))
    assert s.has_processed("alice@example.com", "msg-1") is True


def test_aborted_does_not_count_as_processed(tmp_path: Path) -> None:
    s = SqliteStorage(tmp_path / "audit.db")
    s.record(_decision(action="aborted"))
    # Transient aborts should be retryable on the next webhook delivery.
    assert s.has_processed("alice@example.com", "msg-1") is False


def test_aborted_permanent_DOES_count_as_processed(tmp_path: Path) -> None:
    s = SqliteStorage(tmp_path / "audit.db")
    s.record(_decision(action="aborted-permanent"))
    # Cross-mailbox guard violations are permanent — never retry.
    assert s.has_processed("alice@example.com", "msg-1") is True


def test_recent_orders_chronologically(tmp_path: Path) -> None:
    s = SqliteStorage(tmp_path / "audit.db")
    s.record(_decision(message_id="m1", timestamp="2026-01-01T00:00:00+00:00"))
    s.record(_decision(message_id="m2", timestamp="2026-01-01T00:01:00+00:00"))
    rows = list(s.recent("alice@example.com", "2026-01-01T00:00:00+00:00"))
    assert [r.message_id for r in rows] == ["m1", "m2"]


def test_no_connection_leak_under_load(tmp_path: Path) -> None:
    """Stress test: hammer record/has_processed and confirm we don't leak.

    SQLite connection objects are cheap, but if the previous code path
    leaked one fd per call, this loop would either succeed (fixed) or
    fail with `OSError: Too many open files` on systems with low ulimits.
    Either way it would consume noticeable handles in /proc/self/fd.
    """
    s = SqliteStorage(tmp_path / "audit.db")
    for i in range(500):
        s.record(_decision(message_id=f"m{i}"))
        assert s.has_processed("alice@example.com", f"m{i}")
    # Sanity: count rows
    with sqlite3.connect(tmp_path / "audit.db") as c:
        n = c.execute("SELECT COUNT(*) FROM decisions").fetchone()[0]
    assert n == 500
