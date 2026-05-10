"""Tests for the resource-path parser used to extract user/message ids from notifications."""

from __future__ import annotations

from mailtriage.webhook import _parse_resource


def test_canonical_form() -> None:
    user, msg = _parse_resource("/users/abc-123/mailFolders('Inbox')/messages/AAMk-001")
    assert user == "abc-123"
    assert msg == "AAMk-001"


def test_no_leading_slash() -> None:
    user, msg = _parse_resource("Users/abc-123/MailFolders('Inbox')/Messages/AAMk-001")
    assert user == "abc-123"
    assert msg == "AAMk-001"


def test_short_form() -> None:
    user, msg = _parse_resource("/users/abc-123/messages/AAMk-001")
    assert user == "abc-123"
    assert msg == "AAMk-001"


def test_garbage_returns_none() -> None:
    assert _parse_resource("bogus") == (None, None)
    assert _parse_resource("") == (None, None)
