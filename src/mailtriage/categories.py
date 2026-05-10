"""Category configuration. Single source of truth for the category enum,
the category-to-folder map, and the LLM prompt's category descriptions."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_DESCRIPTION_MAX_LEN = 200

# The literal folder value (case-insensitive) that means "do not move;
# leave in Inbox".
INBOX_SENTINEL = "Inbox"

# Required category name — the system uses this whenever classification
# fails or returns an unknown value, so it MUST exist in categories.txt.
REQUIRED_CATEGORY = "triage"

# Tokens forbidden in description text. Descriptions are interpolated
# into the LLM system prompt; anyone with file-write access could
# otherwise inject instructions. Forbid the obvious shapes.
_FORBIDDEN_DESCRIPTION_TOKENS = ("<", ">", "{", "}", "```")


@dataclass(frozen=True)
class Category:
    name: str
    folder: str
    description: str

    @property
    def is_keep(self) -> bool:
        return self.folder.strip().lower() == INBOX_SENTINEL.lower()


# -------- Parser --------


def parse_categories(path: Path) -> list[Category]:
    if not path.exists():
        raise FileNotFoundError(f"categories file not found: {path}")
    out: list[Category] = []
    # utf-8-sig consumes a leading BOM if a Windows editor saved the file
    # with one, which would otherwise turn the first line into a parse error.
    for i, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(_parse_one(line, i))
        except ValueError as e:
            log.error("categories.txt line %d: %s -- skipping", i, e)
    return out


def _parse_one(line: str, line_no: int) -> Category:
    if "|" not in line:
        raise ValueError("missing '|' separator before description")
    left, _, description = line.partition("|")
    description = description.strip()
    if not description:
        raise ValueError("description (right of '|') is required")
    if len(description) > _DESCRIPTION_MAX_LEN:
        raise ValueError(
            f"description exceeds {_DESCRIPTION_MAX_LEN} chars; trim it"
        )
    for tok in _FORBIDDEN_DESCRIPTION_TOKENS:
        if tok in description:
            raise ValueError(
                f"description contains forbidden token {tok!r}; descriptions "
                f"are rendered into the LLM system prompt and must not contain "
                f"shell/markup characters"
            )
    parts = left.strip().split(None, 1)
    if len(parts) != 2:
        raise ValueError(f"expected '<category-name> <folder-name>' before '|', got: {left!r}")
    name, folder = parts[0].lower(), parts[1].strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"category name {name!r} must match {_NAME_RE.pattern}")
    if not folder:
        raise ValueError("folder name is required")
    return Category(name=name, folder=folder, description=description)


def validate_consistency(categories: list[Category]) -> None:
    """Sanity checks. Raises ValueError if anything is wrong."""
    names = [c.name for c in categories]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ValueError(f"duplicate category names: {sorted(dupes)}")
    if REQUIRED_CATEGORY not in names:
        raise ValueError(
            f"the {REQUIRED_CATEGORY!r} category is required (used as fallback for failures)"
        )
    if not categories:
        raise ValueError("categories.txt has no usable rows")


def render_for_prompt(categories: list[Category]) -> str:
    lines = ["## Categories", ""]
    for c in categories:
        lines.append(f"- `{c.name}` — {c.description}")
    return "\n".join(lines)


# -------- Module-level cache (overridable for tests) --------


_categories: list[Category] | None = None


def load() -> list[Category]:
    global _categories
    if _categories is None:
        from .config import get_settings
        s = get_settings()
        cats = parse_categories(s.categories_file)
        validate_consistency(cats)
        _categories = cats
        log.info("loaded %d categories: %s", len(cats), [c.name for c in cats])
    return _categories


def reset_cache() -> None:
    global _categories
    _categories = None


def set_for_tests(cats: list[Category]) -> None:
    global _categories
    validate_consistency(cats)
    _categories = cats


def valid_names() -> set[str]:
    return {c.name for c in load()}


def folder_for(category_name: str) -> str | None:
    for c in load():
        if c.name == category_name:
            return c.folder
    return None


def is_keep_folder(folder_name: str) -> bool:
    return (folder_name or "").strip().lower() == INBOX_SENTINEL.lower()
