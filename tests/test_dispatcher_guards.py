"""Tests for the cross-mailbox guard. THIS IS A SAFETY-CRITICAL MODULE."""

from __future__ import annotations

import pytest

from mailtriage import categories as cats
from mailtriage.dispatcher import (
    CrossMailboxError,
    Dispatcher,
    UnknownCategoryError,
)


class FakeGraph:
    def __init__(self, folders_by_user: dict[str, dict[str, str]]) -> None:
        self.folders_by_user = folders_by_user
        self.moves: list[tuple[str, str, str]] = []

    def list_folders(self, user_id: str) -> dict[str, str]:
        return dict(self.folders_by_user.get(user_id, {}))

    def move_message(self, user_id: str, message_id: str, dest_folder_id: str) -> dict:
        self.moves.append((user_id, message_id, dest_folder_id))
        return {}


def make_dispatcher(folders_by_user: dict[str, dict[str, str]]) -> tuple[Dispatcher, FakeGraph]:
    fg = FakeGraph(folders_by_user)
    return Dispatcher(fg), fg


# Default test categories live in conftest.py:
#   keep         -> Inbox          (no-op)
#   newsletter   -> Newsletters
#   notification -> Notifications
#   receipt      -> Receipts
#   action-item  -> Action Items
#   triage       -> Triage
#   spam         -> Junk Email


def test_normal_move_within_mailbox() -> None:
    d, fg = make_dispatcher({
        "user-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"},
    })
    msg = {"parentFolderId": "fA-inbox"}
    action, dest = d.dispatch(
        user_id="user-A", message_id="m1", message=msg, category="newsletter",
    )
    assert action == "moved"
    assert dest == "Newsletters"
    assert fg.moves == [("user-A", "m1", "fA-news")]


def test_keep_is_a_no_op() -> None:
    d, fg = make_dispatcher({"user-A": {"Inbox": "fA-inbox"}})
    action, dest = d.dispatch(
        user_id="user-A", message_id="m1", message={"parentFolderId": "fA-inbox"},
        category="keep",
    )
    assert action == "kept"
    assert dest is None
    assert fg.moves == []


def test_unknown_category_raises() -> None:
    d, _ = make_dispatcher({"user-A": {"Inbox": "fA-inbox"}})
    with pytest.raises(UnknownCategoryError):
        d.dispatch(user_id="user-A", message_id="m1", message={}, category="bogus")


def test_cross_mailbox_destination_blocked() -> None:
    """If user-A's folder map gets corrupted to point a folder name at user-B's
    folder ID, the move MUST be aborted."""
    d, fg = make_dispatcher({
        "user-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"},
        "user-B": {"Newsletters": "fB-news", "Inbox": "fB-inbox"},
    })
    d.folder_map("user-A")
    d.folder_map("user-B")

    # Corrupt user-A's map to point Newsletters at user-B's folder id.
    d._maps["user-A"].by_name["Newsletters"] = "fB-news"

    with pytest.raises(CrossMailboxError):
        d.dispatch(
            user_id="user-A", message_id="m1",
            message={"parentFolderId": "fA-inbox"}, category="newsletter",
        )
    assert fg.moves == [], "no move should have been issued"


def test_cross_mailbox_source_blocked() -> None:
    """If a notification claims to be for user-A but the message's
    parentFolderId is in user-B (state corruption / spoofed notification),
    block the move."""
    d, fg = make_dispatcher({
        "user-A": {"Newsletters": "fA-news", "Inbox": "fA-inbox"},
        "user-B": {"Newsletters": "fB-news", "Inbox": "fB-inbox"},
    })
    d.folder_map("user-A")
    d.folder_map("user-B")

    msg = {"parentFolderId": "fB-inbox"}
    with pytest.raises(CrossMailboxError):
        d.dispatch(user_id="user-A", message_id="m1", message=msg, category="newsletter")
    assert fg.moves == []


def test_missing_folder_then_refresh() -> None:
    """If a folder isn't in the cache initially, a fresh fetch should pick it up."""
    fg = FakeGraph({"user-A": {}})
    d = Dispatcher(fg)
    d.folder_map("user-A")  # empty cache
    fg.folders_by_user["user-A"] = {"Newsletters": "fA-news"}
    action, dest = d.dispatch(
        user_id="user-A", message_id="m1",
        message={}, category="newsletter",
    )
    assert action == "moved"
    assert dest == "Newsletters"


def test_new_categories_picked_up_dynamically() -> None:
    """Adding a new category via categories.set_for_tests should be reflected
    immediately — proves the dispatcher is not using a hardcoded mapping."""
    cats.reset_cache()
    cats.set_for_tests([
        cats.Category("triage", "Triage", "x"),
        cats.Category("promotions", "Promotions", "promos"),
    ])
    d, fg = make_dispatcher({
        "user-A": {"Promotions": "fA-promo", "Inbox": "fA-inbox"},
    })
    action, dest = d.dispatch(
        user_id="user-A", message_id="m1",
        message={"parentFolderId": "fA-inbox"}, category="promotions",
    )
    assert action == "moved" and dest == "Promotions"
    assert fg.moves == [("user-A", "m1", "fA-promo")]
