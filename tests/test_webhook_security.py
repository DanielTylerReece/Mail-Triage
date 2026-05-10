"""Security-focused tests for the webhook receiver.

Covers the defense-in-depth guards added after the security audit:
- constant-time clientState comparison
- non-dict body rejection
- body size limit
- message_id shape validation (OData injection defense)
- user_id scope check
"""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _set_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TENANT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("CLIENT_ID", "00000000-0000-0000-0000-000000000000")
    monkeypatch.setenv("CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("WEBHOOK_URL", "https://example.com/graph-webhook")
    monkeypatch.setenv("WEBHOOK_CLIENT_STATE", "test-client-state-value")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    from mailtriage import config as cfg
    cfg.reset_for_tests()
    yield
    cfg.reset_for_tests()


class _FakeQueue:
    def __init__(self):
        self.puts: list = []

    async def put(self, job):
        self.puts.append(job)

    async def get(self):
        raise NotImplementedError

    async def done(self, job):
        pass


def _client(allowed_user_ids=None):
    from mailtriage.webhook import make_app
    q = _FakeQueue()
    app = make_app(q, allowed_user_ids=allowed_user_ids)
    return TestClient(app), q


def test_validation_handshake_echoes_token():
    client, _ = _client()
    r = client.post("/graph-webhook?validationToken=abc123")
    assert r.status_code == 200
    assert r.text == "abc123"
    assert r.headers["content-type"].startswith("text/plain")


def test_clientstate_mismatch_drops_notification():
    client, q = _client()
    payload = {"value": [{
        "clientState": "wrong-secret",
        "resource": "/users/u-1/mailFolders('Inbox')/messages/AAA",
    }]}
    r = client.post("/graph-webhook", json=payload)
    assert r.status_code == 202
    assert q.puts == []


def test_clientstate_match_queues_job():
    client, q = _client()
    payload = {"value": [{
        "clientState": "test-client-state-value",
        "resource": "/users/u-1/mailFolders('Inbox')/messages/AAA",
    }]}
    r = client.post("/graph-webhook", json=payload)
    assert r.status_code == 202
    assert len(q.puts) == 1


def test_non_dict_body_rejected():
    client, q = _client()
    r = client.post("/graph-webhook", json=["not", "a", "dict"])
    assert r.status_code == 400
    assert q.puts == []


def test_non_json_body_rejected():
    client, q = _client()
    r = client.post("/graph-webhook", content=b"not json", headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert q.puts == []


def test_body_size_limit_enforced():
    client, q = _client()
    big = b'{"x":"' + (b"A" * (200 * 1024)) + b'"}'
    r = client.post("/graph-webhook", content=big, headers={"content-type": "application/json"})
    assert r.status_code == 413
    assert q.puts == []


def test_message_id_with_query_chars_rejected():
    """OData injection attempt — message_id contains '?' via fallback parser."""
    client, q = _client()
    payload = {"value": [{
        "clientState": "test-client-state-value",
        "resource": "/users/u-1/messages/AAA?$expand=attachments",
    }]}
    r = client.post("/graph-webhook", json=payload)
    assert r.status_code == 202   # we still ack to Microsoft
    assert q.puts == []           # but the malformed message_id is dropped


def test_message_id_with_slashes_dropped():
    client, q = _client()
    payload = {"value": [{
        "clientState": "test-client-state-value",
        "resource": "/users/u-1/messages/../subscriptions",
    }]}
    r = client.post("/graph-webhook", json=payload)
    assert r.status_code == 202
    assert q.puts == []


def test_unknown_user_id_dropped_when_scope_set():
    client, q = _client(allowed_user_ids={"u-good"})
    payload = {"value": [{
        "clientState": "test-client-state-value",
        "resource": "/users/u-attacker/mailFolders('Inbox')/messages/AAA",
    }]}
    r = client.post("/graph-webhook", json=payload)
    assert r.status_code == 202
    assert q.puts == []


def test_known_user_id_allowed_when_scope_set():
    client, q = _client(allowed_user_ids={"u-good"})
    payload = {"value": [{
        "clientState": "test-client-state-value",
        "resource": "/users/u-good/mailFolders('Inbox')/messages/AAA",
    }]}
    r = client.post("/graph-webhook", json=payload)
    assert r.status_code == 202
    assert len(q.puts) == 1


def test_healthz():
    client, _ = _client()
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_non_dict_notification_in_value_skipped():
    """Each item in `body['value']` must itself be a dict; bad ones skipped."""
    client, q = _client()
    payload = {"value": [
        "not a dict",
        {"clientState": "test-client-state-value",
         "resource": "/users/u-1/mailFolders('Inbox')/messages/AAA"},
        42,
    ]}
    r = client.post("/graph-webhook", json=payload)
    assert r.status_code == 202
    assert len(q.puts) == 1
