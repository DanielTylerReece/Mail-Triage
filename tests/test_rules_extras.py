"""Extra rules tests for the parser fixes from the audit:
- tabs in mailbox: prefix
- subject-regex compiled at parse time
- from-domain doesn't match unrelated suffixes
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mailtriage import rules as r


def w(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "rules.txt"
    p.write_text(content)
    return p


def msg(*, sender: str = "alex@example.com", subject: str = "hi") -> dict:
    return {"from": {"emailAddress": {"address": sender}}, "subject": subject}


def test_mailbox_prefix_with_tabs(tmp_path: Path) -> None:
    rules = r.parse_rules(w(tmp_path, "mailbox:alice@x.com\tfrom-domain x.com -> spam\n"))
    assert len(rules) == 1
    assert rules[0].mailbox == "alice@x.com"


def test_subject_regex_invalid_pattern_rejected_at_parse_time(tmp_path: Path) -> None:
    # Previously this swallowed the re.error at evaluate time and silently never matched.
    rules = r.parse_rules(w(tmp_path, "subject-regex *invalid( -> spam\n"))
    assert rules == []


def test_subject_regex_compiles_at_parse(tmp_path: Path) -> None:
    rules = r.parse_rules(w(tmp_path, r"subject-regex ^Order #\d+ -> receipt" + "\n"))
    assert rules[0].compiled_regex is not None
    assert r.evaluate(rules, "x", msg(subject="Order #123")).matched
    assert not r.evaluate(rules, "x", msg(subject="hello")).matched


def test_from_domain_does_not_match_unrelated_suffix(tmp_path: Path) -> None:
    rules = r.parse_rules(w(tmp_path, "from-domain example.com -> spam\n"))
    # `notexample.com` should NOT match `example.com`.
    assert not r.evaluate(rules, "x", msg(sender="bad@notexample.com")).matched
    # Subdomains DO match (documented behavior).
    assert r.evaluate(rules, "x", msg(sender="ok@mail.example.com")).matched
    # Exact domain matches.
    assert r.evaluate(rules, "x", msg(sender="ok@example.com")).matched


def test_categories_inbox_sentinel_case_insensitive() -> None:
    from mailtriage import categories as cats
    cats.reset_cache()
    cats.set_for_tests([
        cats.Category("keep_lower", "inbox", "x"),
        cats.Category("keep_caps",  "INBOX", "x"),
        cats.Category("keep_norm",  "Inbox", "x"),
        cats.Category("not_keep",   "Newsletters", "x"),
        cats.Category("triage",     "Triage", "x"),
    ])
    cs = {c.name: c for c in cats.load()}
    assert cs["keep_lower"].is_keep
    assert cs["keep_caps"].is_keep
    assert cs["keep_norm"].is_keep
    assert not cs["not_keep"].is_keep
    assert cats.is_keep_folder("Inbox")
    assert cats.is_keep_folder("INBOX")
    assert cats.is_keep_folder("inbox")
    assert not cats.is_keep_folder("Newsletters")
