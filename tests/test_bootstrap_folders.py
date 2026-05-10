"""Tests for the bootstrap_folders helper."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from mailtriage import bootstrap_folders


class _FakeGraph:
    def __init__(self, folders_by_addr, resolve_404=()):
        self.folders_by_addr = folders_by_addr
        self.resolve_404 = set(resolve_404)
        self.posted: list[tuple[str, dict]] = []

    def resolve_mailbox(self, addr):
        if addr in self.resolve_404:
            from mailtriage.graph import GraphError
            raise GraphError("GET", f"/users/{addr}", 404, "not found")
        from mailtriage.graph import Mailbox
        return Mailbox(listed_address=addr, user_id=f"uid-{addr}", primary_upn=addr)

    def list_folders(self, user_id):
        addr = user_id.replace("uid-", "")
        return {name: f"fid-{name}" for name in self.folders_by_addr.get(addr, [])}

    def _post(self, path, body):
        self.posted.append((path, body))
        return {"id": f"new-{body['displayName']}"}

    def close(self):
        pass


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("TENANT_ID", "x")
    monkeypatch.setenv("CLIENT_ID", "x")
    monkeypatch.setenv("CLIENT_SECRET", "y")
    monkeypatch.setenv("WEBHOOK_URL", "https://x")
    monkeypatch.setenv("WEBHOOK_CLIENT_STATE", "y")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ok")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    (tmp_path / "mailboxes.txt").write_text(
        "alice@example.com\nbob@example.com\nshared@example.com\n"
    )
    (tmp_path / "categories.txt").write_text(
        "keep         Inbox          | leave\n"
        "newsletter   Newsletters    | bulk\n"
        "triage       Triage         | uncertain\n"
    )
    from mailtriage import categories as cats
    from mailtriage import config as cfg
    cfg.reset_for_tests()
    # Override the autouse default category fixture so bootstrap_folders
    # sees the test's small categories.txt content via cats.load().
    cats.reset_cache()
    cats.set_for_tests([
        cats.Category("keep", "Inbox", "leave"),
        cats.Category("newsletter", "Newsletters", "bulk"),
        cats.Category("triage", "Triage", "uncertain"),
    ])
    yield tmp_path
    cfg.reset_for_tests()
    cats.reset_cache()


def test_creates_missing_folders(env, monkeypatch):
    fake = _FakeGraph(
        folders_by_addr={
            "alice@example.com": ["Newsletters", "Triage"],
            "bob@example.com": [],
        },
        resolve_404=["shared@example.com"],
    )
    monkeypatch.setattr(bootstrap_folders, "GraphClient", lambda: fake)
    monkeypatch.setattr("sys.argv", ["bootstrap_folders"])
    rc = bootstrap_folders.main()
    assert rc == 0
    # alice has both already → no creates
    # bob missing both → 2 creates
    # shared 404 → skipped
    assert len(fake.posted) == 2
    created_names = {body["displayName"] for _, body in fake.posted}
    assert created_names == {"Newsletters", "Triage"}
    assert all(p.startswith("/users/uid-bob@example.com/mailFolders") for p, _ in fake.posted)


def test_dry_run_does_not_create(env, monkeypatch):
    fake = _FakeGraph(folders_by_addr={"alice@example.com": [], "bob@example.com": [], "shared@example.com": []})
    monkeypatch.setattr(bootstrap_folders, "GraphClient", lambda: fake)
    monkeypatch.setattr("sys.argv", ["bootstrap_folders", "--dry-run"])
    rc = bootstrap_folders.main()
    assert rc == 0
    assert fake.posted == []


def test_inbox_sentinel_categories_skipped(env, monkeypatch, tmp_path):
    # Only `keep` (Inbox sentinel) and the required `triage` — nothing to create.
    from mailtriage import categories as cats
    cats.reset_cache()
    cats.set_for_tests([
        cats.Category("keep", "Inbox", "x"),
        cats.Category("triage", "Triage", "y"),
    ])
    fake = _FakeGraph(folders_by_addr={"alice@example.com": ["Triage"], "bob@example.com": ["Triage"], "shared@example.com": ["Triage"]})
    monkeypatch.setattr(bootstrap_folders, "GraphClient", lambda: fake)
    monkeypatch.setattr("sys.argv", ["bootstrap_folders"])
    rc = bootstrap_folders.main()
    assert rc == 0
    assert fake.posted == []
