import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mailtriage import categories as cats  # noqa: E402
from mailtriage import classifier as classifier_mod  # noqa: E402
from mailtriage import config as cfg  # noqa: E402

# Default category set for tests. Tests that need a custom set can override
# via cats.set_for_tests(...) inside the test.
_DEFAULT_TEST_CATEGORIES = [
    cats.Category("keep",         "Inbox",         "leave in inbox"),
    cats.Category("newsletter",   "Newsletters",   "bulk subscription content"),
    cats.Category("notification", "Notifications", "machine notifications"),
    cats.Category("receipt",      "Receipts",      "order/payment receipts"),
    cats.Category("action-item",  "Action Items",  "needs response"),
    cats.Category("triage",       "Triage",        "uncertain — review by hand"),
    cats.Category("spam",         "Junk Email",    "phishing or scam"),
]


@pytest.fixture(autouse=True)
def _isolate_category_state():
    """Reset the categories cache before and after every test so test order
    doesn't matter."""
    cats.reset_cache()
    cats.set_for_tests(list(_DEFAULT_TEST_CATEGORIES))
    yield
    cats.reset_cache()


@pytest.fixture(autouse=True)
def _isolate_settings():
    """Settings is a module-level singleton. Reset between tests so a test
    that injects env vars doesn't poison subsequent tests."""
    cfg.reset_for_tests()
    yield
    cfg.reset_for_tests()


@pytest.fixture(autouse=True)
def _isolate_classifier():
    """The classifier caches the resolved provider AND the rendered system
    prompt at module level. Without resetting, a test that triggers either
    poisons every subsequent test."""
    classifier_mod.reset_provider_for_tests()
    classifier_mod.reset_prompt_cache_for_tests()
    yield
    classifier_mod.reset_provider_for_tests()
    classifier_mod.reset_prompt_cache_for_tests()
