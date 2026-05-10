"""Tier 1 rules engine. Plain text rules file, first match wins.

Valid categories are loaded dynamically from categories.txt — adding a new
category does not require code changes here.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import categories as cats

log = logging.getLogger(__name__)

VALID_TYPES = {
    "from", "from-domain", "subject-contains", "subject-regex",
    "header-exists", "to", "self-loop",
}

# Whitespace splitter used everywhere we tokenize a rule line. Tolerates
# spaces, tabs, and runs of either.
_WS = re.compile(r"\s+")


@dataclass
class Rule:
    mailbox: str | None
    match_type: str
    pattern: str
    category: str
    line_no: int
    raw: str
    # Pre-compiled regex for subject-regex rules; populated at parse time
    # so a typo'd pattern surfaces as a parse-time error rather than
    # silently never matching.
    compiled_regex: re.Pattern | None = None

    def matches(self, mailbox: str, msg: dict[str, Any]) -> bool:
        if self.mailbox is not None and self.mailbox != mailbox.lower():
            return False
        return _evaluate(self, mailbox, msg)


def parse_rules(path: Path) -> list[Rule]:
    if not path.exists():
        log.warning("rules file %s does not exist; running with empty rule set", path)
        return []
    valid_categories = cats.valid_names()
    rules: list[Rule] = []
    # utf-8-sig: silently consume a BOM that a Windows editor may have left.
    for i, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rules.append(_parse_one(line, i, valid_categories))
        except ValueError as e:
            log.error("rules.txt line %d: %s -- skipping", i, e)
    log.info("loaded %d rules", len(rules))
    return rules


def _parse_one(line: str, line_no: int, valid_categories: set[str]) -> Rule:
    mailbox: str | None = None
    if line.startswith("mailbox:"):
        # Split on any whitespace (space OR tab), not just literal spaces.
        bits = _WS.split(line, maxsplit=1)
        if len(bits) != 2:
            raise ValueError(f"missing rule body after {bits[0]!r}")
        mailbox = bits[0].removeprefix("mailbox:").lower()
        line = bits[1].strip()

    if "->" not in line:
        raise ValueError("missing '->' separator")
    match_part, _, category = line.rpartition("->")
    category = category.strip().lower()
    match_part = match_part.strip()

    if category not in valid_categories:
        raise ValueError(
            f"unknown category {category!r}; valid: {sorted(valid_categories)} "
            f"(add to categories.txt to extend)"
        )

    if match_part == "self-loop":
        match_type, pattern = "self-loop", ""
    else:
        bits = _WS.split(match_part, maxsplit=1)
        if len(bits) != 2:
            raise ValueError(f"match clause {match_part!r} needs a pattern")
        match_type = bits[0].lower()
        pattern = bits[1].strip()
        if match_type not in VALID_TYPES:
            raise ValueError(f"unknown match type {match_type!r}; valid: {sorted(VALID_TYPES)}")
        if not pattern:
            raise ValueError(f"match type {match_type!r} requires a pattern")

    compiled: re.Pattern | None = None
    if match_type == "subject-regex":
        try:
            compiled = re.compile(pattern)
        except re.error as e:
            raise ValueError(f"invalid subject-regex pattern {pattern!r}: {e}") from e

    return Rule(
        mailbox=mailbox, match_type=match_type, pattern=pattern,
        category=category, line_no=line_no, raw=line, compiled_regex=compiled,
    )


def _evaluate(rule: Rule, mailbox: str, msg: dict[str, Any]) -> bool:
    sender = _sender_address(msg).lower()
    subject = (msg.get("subject") or "").strip()
    headers = {h["name"].lower(): h["value"] for h in (msg.get("internetMessageHeaders") or [])}

    mt = rule.match_type
    pattern = rule.pattern

    if mt == "from":
        return sender == pattern.lower()
    if mt == "from-domain":
        # Matches sender's exact domain (alex@example.com) or any subdomain
        # (alex@mail.example.com). Does not match unrelated suffixes
        # (alex@notexample.com → no match).
        return sender.endswith("@" + pattern.lower()) or sender.endswith("." + pattern.lower())
    if mt == "subject-contains":
        return pattern.lower() in subject.lower()
    if mt == "subject-regex":
        if rule.compiled_regex is None:
            return False
        return rule.compiled_regex.search(subject) is not None
    if mt == "header-exists":
        return pattern.lower() in headers
    if mt == "to":
        recips = [
            (r.get("emailAddress") or {}).get("address", "").lower()
            for r in (msg.get("toRecipients") or [])
        ]
        return pattern.lower() in recips
    if mt == "self-loop":
        return sender == mailbox.lower()
    return False


def _sender_address(msg: dict[str, Any]) -> str:
    f = msg.get("from") or {}
    return ((f.get("emailAddress") or {}).get("address") or "")


@dataclass
class Tier1Result:
    matched: bool
    category: str | None = None
    rule: Rule | None = None


def evaluate(rules: list[Rule], mailbox: str, msg: dict[str, Any]) -> Tier1Result:
    for r in rules:
        if r.matches(mailbox, msg):
            return Tier1Result(matched=True, category=r.category, rule=r)
    return Tier1Result(matched=False)
