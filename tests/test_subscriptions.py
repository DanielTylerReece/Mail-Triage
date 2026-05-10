"""Tests for subscription state-file handling — atomic writes, stale detection."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mailtriage.subscriptions import (
    STALE_BUFFER_MINUTES,
    _is_stale,
    _load_state,
    _save_state,
)


def _iso(minutes_from_now: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes_from_now)).isoformat()


def test_is_stale_missing() -> None:
    assert _is_stale(None) is True
    assert _is_stale("") is True


def test_is_stale_malformed() -> None:
    assert _is_stale("not-a-date") is True


def test_is_stale_in_past() -> None:
    assert _is_stale(_iso(-60)) is True


def test_is_stale_within_buffer() -> None:
    assert _is_stale(_iso(STALE_BUFFER_MINUTES - 5)) is True


def test_is_stale_well_in_future() -> None:
    assert _is_stale(_iso(STALE_BUFFER_MINUTES + 60)) is False


def test_save_then_load_roundtrip(tmp_path: Path) -> None:
    p = tmp_path / "subscriptions.json"
    state = {"u1": {"subscription_id": "abc", "expiration": _iso(120), "primary_upn": "x@y.z"}}
    _save_state(p, state)
    assert _load_state(p) == state


def test_save_is_atomic(tmp_path: Path) -> None:
    """The .tmp file should appear and be replaced; the final file must
    contain the full payload, not a half-written intermediate."""
    p = tmp_path / "subscriptions.json"
    _save_state(p, {"a": {"subscription_id": "1"}})
    _save_state(p, {"b": {"subscription_id": "2"}})
    # No leftover .tmp file
    assert not (tmp_path / "subscriptions.json.tmp").exists()
    assert _load_state(p) == {"b": {"subscription_id": "2"}}


def test_load_corrupt_state_recovers(tmp_path: Path) -> None:
    """A corrupt state file (e.g. truncated mid-write) should not crash;
    we treat it as empty and move it aside for diagnosis."""
    p = tmp_path / "subscriptions.json"
    p.write_text('{"u1": {"subscription_id": "ab')  # truncated
    out = _load_state(p)
    assert out == {}
    # Original file should be moved aside
    assert (tmp_path / "subscriptions.json.corrupt").exists()
