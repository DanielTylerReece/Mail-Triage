"""Tests for the stale-folder-id auto-recovery in Dispatcher.dispatch().

Covers the case from the audit: a user deletes a folder and creates a
new one with the same display name (different folder_id). Our cached
folder_map has the dead folder_id; move_message returns 404; we should
refresh the map, find the new id, and retry once.
"""

from __future__ import annotations

import pytest

from mailtriage.dispatcher import CrossMailboxError, Dispatcher
from mailtriage.graph import GraphError


class _FakeGraph:
    def __init__(self, folders_by_user, fail_first_move_with: int | None = None):
        self.folders_by_user = folders_by_user
        self.moves: list[tuple[str, str, str]] = []
        self._fail_with = fail_first_move_with
        self._failed_once = False

    def list_folders(self, user_id: str) -> dict[str, str]:
        return dict(self.folders_by_user.get(user_id, {}))

    def move_message(self, user_id: str, message_id: str, dest_id: str) -> dict:
        if self._fail_with and not self._failed_once:
            self._failed_once = True
            raise GraphError(
                "POST", f"/users/{user_id}/messages/{message_id}/move",
                self._fail_with, "folder not found",
            )
        self.moves.append((user_id, message_id, dest_id))
        return {}


def test_404_on_move_triggers_refresh_and_retry() -> None:
    """User deletes /Newsletters and recreates it (different folder_id).
    Our cache has the dead id; first move fails with 404; we refresh and
    retry against the new id."""
    fg = _FakeGraph(
        folders_by_user={"u-A": {"Newsletters": "fA-news-OLD", "Inbox": "fA-inbox"}},
        fail_first_move_with=404,
    )
    d = Dispatcher(fg)
    # Prime the cache with the OLD folder ids.
    d.folder_map("u-A")

    # Now simulate the folder being deleted+recreated server-side. Update
    # the fake's view of the world.
    fg.folders_by_user["u-A"] = {"Newsletters": "fA-news-NEW", "Inbox": "fA-inbox"}

    action, dest = d.dispatch(
        user_id="u-A", message_id="m1",
        message={"parentFolderId": "fA-inbox"}, category="newsletter",
    )
    assert action == "moved"
    assert dest == "Newsletters"
    # The retry hit the NEW folder id, not the cached OLD one.
    assert fg.moves == [("u-A", "m1", "fA-news-NEW")]


def test_404_with_unchanged_folder_id_propagates() -> None:
    """If the refresh resolves to the SAME folder_id, retrying would just
    fail again — surface the original error."""
    fg = _FakeGraph(
        folders_by_user={"u-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"}},
        fail_first_move_with=404,
    )
    d = Dispatcher(fg)
    d.folder_map("u-A")

    # World hasn't changed; refresh returns the same id.
    with pytest.raises(GraphError) as exc:
        d.dispatch(
            user_id="u-A", message_id="m1",
            message={"parentFolderId": "fA-inbox"}, category="newsletter",
        )
    assert exc.value.status == 404


def test_non_404_error_does_not_retry() -> None:
    fg = _FakeGraph(
        folders_by_user={"u-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"}},
        fail_first_move_with=500,
    )
    d = Dispatcher(fg)
    d.folder_map("u-A")

    with pytest.raises(GraphError) as exc:
        d.dispatch(
            user_id="u-A", message_id="m1",
            message={"parentFolderId": "fA-inbox"}, category="newsletter",
        )
    assert exc.value.status == 500
    assert fg.moves == []  # never retried


def test_404_then_folder_disappears() -> None:
    """User deletes /Newsletters and doesn't recreate it. Refresh fails to
    find the folder → raise GraphError, message goes to triage upstream."""
    fg = _FakeGraph(
        folders_by_user={"u-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"}},
        fail_first_move_with=404,
    )
    d = Dispatcher(fg)
    d.folder_map("u-A")

    fg.folders_by_user["u-A"] = {"Inbox": "fA-inbox"}  # Newsletters gone

    with pytest.raises(GraphError) as exc:
        d.dispatch(
            user_id="u-A", message_id="m1",
            message={"parentFolderId": "fA-inbox"}, category="newsletter",
        )
    assert exc.value.status == 404
    assert "no longer exists" in exc.value.body


def test_404_retry_still_runs_cross_mailbox_guard() -> None:
    """On the retry path, if the refreshed by_name map somehow points to a
    folder_id NOT in the refreshed folder_ids set (corruption pattern from
    test_dispatcher_guards.py::test_cross_mailbox_destination_blocked),
    the guard must still fire."""
    fg = _FakeGraph(
        folders_by_user={"u-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"}},
        fail_first_move_with=404,
    )
    d = Dispatcher(fg)
    d.folder_map("u-A")

    # Move the world forward and corrupt the map post-refresh: by_name
    # points at an id that isn't in folder_ids (same pattern we test in
    # the main guard suite, applied to the retry path).
    fg.folders_by_user["u-A"] = {"Newsletters": "fA-news-NEW", "Inbox": "fA-inbox"}
    original_dispatch = d.dispatch

    def post_refresh_corrupt(user_id, refresh=False):
        fmap = Dispatcher.folder_map(d, user_id, refresh=refresh)
        if refresh:
            fmap.by_name["Newsletters"] = "fELSEWHERE-id-not-in-folder_ids"
        return fmap
    d.folder_map = post_refresh_corrupt  # type: ignore[assignment]

    with pytest.raises(CrossMailboxError):
        original_dispatch(
            user_id="u-A", message_id="m1",
            message={"parentFolderId": "fA-inbox"}, category="newsletter",
        )
    # The first move was the 404; no successful retry move should have happened.
    assert fg.moves == []
