"""Tests for DRY_RUN mode."""

from __future__ import annotations

import pytest

from mailtriage.dispatcher import CrossMailboxError, Dispatcher
from mailtriage.graph import GraphError
from mailtriage.storage import PROCESSED_ACTIONS


class _FakeGraph:
    def __init__(self, folders_by_user):
        self.folders_by_user = folders_by_user
        self.moves: list = []

    def list_folders(self, user_id):
        return dict(self.folders_by_user.get(user_id, {}))

    def move_message(self, user_id, message_id, dest_id):
        self.moves.append((user_id, message_id, dest_id))
        return {}


def test_dry_run_does_not_call_move_message() -> None:
    fg = _FakeGraph({"u-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"}})
    d = Dispatcher(fg, dry_run=True)
    action, dest = d.dispatch(
        user_id="u-A", message_id="m1",
        message={"parentFolderId": "fA-inbox"}, category="newsletter",
    )
    assert action == "dry-run"
    assert dest == "Newsletters"
    assert fg.moves == []


def test_dry_run_still_runs_cross_mailbox_guard() -> None:
    """Even in dry-run, a cross-mailbox violation must be detected and the
    move aborted with CrossMailboxError. The audit row will record
    aborted-permanent (not dry-run), so the operator sees the violation."""
    fg = _FakeGraph({
        "u-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"},
        "u-B": {"Newsletters": "fB-news"},
    })
    d = Dispatcher(fg, dry_run=True)
    d.folder_map("u-A")
    d.folder_map("u-B")
    # Corrupt user-A's by_name to point at user-B's folder id without
    # updating folder_ids — same pattern as test_dispatcher_guards.
    d._maps["u-A"].by_name["Newsletters"] = "fB-news"
    with pytest.raises(CrossMailboxError):
        d.dispatch(
            user_id="u-A", message_id="m1",
            message={"parentFolderId": "fA-inbox"}, category="newsletter",
        )
    assert fg.moves == []


def test_dry_run_keep_is_still_kept() -> None:
    """The 'keep' (Inbox) sentinel returns 'kept', not 'dry-run'. Dry-run
    only affects move actions; keep was already a no-op."""
    fg = _FakeGraph({"u-A": {"Inbox": "fA-inbox"}})
    d = Dispatcher(fg, dry_run=True)
    action, dest = d.dispatch(
        user_id="u-A", message_id="m1",
        message={"parentFolderId": "fA-inbox"}, category="keep",
    )
    assert action == "kept"
    assert dest is None
    assert fg.moves == []


def test_normal_mode_default() -> None:
    """Without dry_run=True, dispatcher defaults to live action."""
    fg = _FakeGraph({"u-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"}})
    d = Dispatcher(fg)  # dry_run not set -> False
    action, _ = d.dispatch(
        user_id="u-A", message_id="m1",
        message={"parentFolderId": "fA-inbox"}, category="newsletter",
    )
    assert action == "moved"
    assert len(fg.moves) == 1


def test_dry_run_action_counts_as_processed() -> None:
    """A 'dry-run' audit row should be treated as 'this message was seen
    and resolved' so a webhook redelivery doesn't replay the pipeline.
    Important when transitioning out of dry-run: old dry-run mail stays
    where it is (in Inbox), new mail is real-classified."""
    assert "dry-run" in PROCESSED_ACTIONS


def test_dry_run_setting_default_is_false(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TENANT_ID", "x")
    monkeypatch.setenv("CLIENT_ID", "x")
    monkeypatch.setenv("CLIENT_SECRET", "y")
    monkeypatch.setenv("WEBHOOK_URL", "https://x")
    monkeypatch.setenv("WEBHOOK_CLIENT_STATE", "y")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ok")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # No DRY_RUN env var set
    monkeypatch.delenv("DRY_RUN", raising=False)
    from mailtriage.config import get_settings, reset_for_tests
    reset_for_tests()
    s = get_settings()
    assert s.dry_run is False
    reset_for_tests()


def test_dry_run_setting_parses_truthy(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TENANT_ID", "x")
    monkeypatch.setenv("CLIENT_ID", "x")
    monkeypatch.setenv("CLIENT_SECRET", "y")
    monkeypatch.setenv("WEBHOOK_URL", "https://x")
    monkeypatch.setenv("WEBHOOK_CLIENT_STATE", "y")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ok")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("DRY_RUN", "true")
    from mailtriage.config import get_settings, reset_for_tests
    reset_for_tests()
    s = get_settings()
    assert s.dry_run is True
    reset_for_tests()
