"""Tests for the Tier 1 rules engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailtriage import rules as r


def write_rules(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "rules.txt"
    p.write_text(content)
    return p


def msg(*, sender: str = "alex@example.com", subject: str = "hi",
        to: list[str] | None = None, headers: dict[str, str] | None = None) -> dict:
    return {
        "from": {"emailAddress": {"address": sender, "name": ""}},
        "subject": subject,
        "toRecipients": [{"emailAddress": {"address": a}} for a in (to or ["me@me.com"])],
        "internetMessageHeaders": [{"name": k, "value": v} for k, v in (headers or {}).items()],
    }


def test_parse_simple_rule(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, "from-domain example.com -> newsletter\n"))
    assert len(rules) == 1
    assert rules[0].mailbox is None
    assert rules[0].match_type == "from-domain"
    assert rules[0].pattern == "example.com"
    assert rules[0].category == "newsletter"


def test_parse_mailbox_scoped_rule(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(
        tmp_path, "mailbox:alice@x.com from-domain fortinet.com -> action-item\n"
    ))
    assert rules[0].mailbox == "alice@x.com"


def test_parse_skips_comments_and_blanks(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, "# a comment\n\n\nfrom-domain x.com -> spam\n"))
    assert len(rules) == 1


def test_invalid_category_skipped(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, "from-domain x.com -> nonsense\n"))
    assert rules == []


def test_invalid_match_type_skipped(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, "from-mars alien -> spam\n"))
    assert rules == []


def test_first_match_wins(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, """
from-domain example.com -> newsletter
from alex@example.com -> keep
""".strip()))
    result = r.evaluate(rules, "me@me.com", msg(sender="alex@example.com"))
    assert result.matched
    assert result.category == "newsletter"


def test_self_loop(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, "self-loop -> notification\n"))
    res = r.evaluate(rules, "alice@me.com", msg(sender="alice@me.com"))
    assert res.matched and res.category == "notification"


def test_mailbox_scoped_rule_only_matches_target(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(
        tmp_path, "mailbox:alice@me.com from-domain fortinet.com -> action-item\n"
    ))
    m = msg(sender="alerts@fortinet.com")
    assert r.evaluate(rules, "alice@me.com", m).matched
    assert not r.evaluate(rules, "bob@me.com", m).matched


def test_header_exists(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, "header-exists List-Unsubscribe -> newsletter\n"))
    assert r.evaluate(rules, "x", msg(headers={"List-Unsubscribe": "<x>"})).matched
    assert not r.evaluate(rules, "x", msg(headers={})).matched


def test_subject_regex(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, r"subject-regex ^Order #\d+ -> receipt" + "\n"))
    assert r.evaluate(rules, "x", msg(subject="Order #12345 confirmed")).matched
    assert not r.evaluate(rules, "x", msg(subject="hello")).matched


def test_no_match_returns_unmatched(tmp_path: Path) -> None:
    rules = r.parse_rules(write_rules(tmp_path, "from-domain example.com -> newsletter\n"))
    res = r.evaluate(rules, "x", msg(sender="bob@other.com"))
    assert not res.matched
    assert res.category is None
