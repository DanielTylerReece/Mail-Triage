"""Tests for categories.txt parsing and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from mailtriage import categories as cats


def write_cats(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "categories.txt"
    p.write_text(content)
    return p


def test_parses_simple_file(tmp_path: Path) -> None:
    p = write_cats(tmp_path, """
keep         Inbox         | leave in inbox
triage       Triage        | uncertain
spam         Junk Email    | obvious scam
""".strip())
    out = cats.parse_categories(p)
    assert [c.name for c in out] == ["keep", "triage", "spam"]
    assert out[0].folder == "Inbox"
    assert out[0].is_keep
    assert out[2].folder == "Junk Email"


def test_skips_comments_and_blanks(tmp_path: Path) -> None:
    p = write_cats(tmp_path, """
# header comment

keep    Inbox  | a
   # indented comment
triage  Triage | b
""")
    out = cats.parse_categories(p)
    assert len(out) == 2


def test_missing_pipe_separator_skipped(tmp_path: Path) -> None:
    p = write_cats(tmp_path, "keep Inbox no pipe here\ntriage Triage | ok\n")
    out = cats.parse_categories(p)
    assert [c.name for c in out] == ["triage"]


def test_missing_description_skipped(tmp_path: Path) -> None:
    p = write_cats(tmp_path, "keep Inbox |   \ntriage Triage | ok\n")
    out = cats.parse_categories(p)
    assert [c.name for c in out] == ["triage"]


def test_invalid_name_skipped(tmp_path: Path) -> None:
    # Names are normalized to lowercase, so "MIXED-case" is fine. But chars
    # outside [a-z0-9_-] (like punctuation) reject the row entirely.
    p = write_cats(tmp_path, "foo!bar Inbox | nope\nok ok-folder | y\n")
    out = cats.parse_categories(p)
    assert [c.name for c in out] == ["ok"]


def test_uppercase_name_normalized(tmp_path: Path) -> None:
    p = write_cats(tmp_path, "ALPHA Inbox | x\ntriage Triage | y\n")
    out = cats.parse_categories(p)
    assert [c.name for c in out] == ["alpha", "triage"]


def test_validate_rejects_duplicates(tmp_path: Path) -> None:
    cats_list = [
        cats.Category("keep", "Inbox", "x"),
        cats.Category("keep", "Other", "y"),
        cats.Category("triage", "Triage", "z"),
    ]
    with pytest.raises(ValueError, match="duplicate"):
        cats.validate_consistency(cats_list)


def test_validate_requires_triage() -> None:
    cats_list = [cats.Category("keep", "Inbox", "x")]
    with pytest.raises(ValueError, match="triage"):
        cats.validate_consistency(cats_list)


def test_validate_rejects_empty() -> None:
    with pytest.raises(ValueError):
        cats.validate_consistency([])


def test_render_for_prompt_lists_all_categories() -> None:
    cats_list = [
        cats.Category("keep", "Inbox", "leave in inbox"),
        cats.Category("triage", "Triage", "uncertain"),
    ]
    out = cats.render_for_prompt(cats_list)
    assert "## Categories" in out
    assert "`keep`" in out and "leave in inbox" in out
    assert "`triage`" in out and "uncertain" in out


def test_accessors_use_loaded_set(tmp_path: Path) -> None:
    cats.reset_cache()
    cats.set_for_tests([
        cats.Category("alpha", "Folder-A", "a"),
        cats.Category("beta",  "Folder-B", "b"),
        cats.Category("triage", "Triage",  "t"),
    ])
    assert cats.valid_names() == {"alpha", "beta", "triage"}
    assert cats.folder_for("alpha") == "Folder-A"
    assert cats.folder_for("nope") is None


def test_inbox_sentinel_means_keep() -> None:
    c_keep = cats.Category("keep", "Inbox", "x")
    c_move = cats.Category("news", "Newsletters", "y")
    assert c_keep.is_keep is True
    assert c_move.is_keep is False
