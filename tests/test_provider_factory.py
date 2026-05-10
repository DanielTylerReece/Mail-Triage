"""Provider factory tests — covers anthropic, openai, openai-compatible."""

from __future__ import annotations

import pytest

from mailtriage import classifier as classifier_mod
from mailtriage import config as cfg


def _base_env(monkeypatch, tmp_path, **overrides):
    base = {
        "TENANT_ID": "t",
        "CLIENT_ID": "c",
        "CLIENT_SECRET": "s",
        "WEBHOOK_URL": "https://example.com/graph-webhook",
        "WEBHOOK_CLIENT_STATE": "x" * 32,
        "DATA_DIR": str(tmp_path / "data"),
        "CONFIG_DIR": str(tmp_path / "config"),
    }
    base.update(overrides)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    for k in (
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
        "LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL",
        "LLM_RESPONSE_FORMAT_JSON",
    ):
        if k not in base:
            monkeypatch.delenv(k, raising=False)


class _FakeOpenAI:
    last_init_kwargs: dict | None = None
    last_call_kwargs: dict | None = None

    def __init__(self, **kwargs):
        type(self).last_init_kwargs = kwargs
        self.chat = self
        self.completions = self

    def create(self, **kwargs):
        type(self).last_call_kwargs = kwargs
        class _Msg:
            content = '{"category":"keep","confidence":0.9,"reasoning":"r"}'
        class _Choice:
            message = _Msg()
        class _Resp:
            choices = [_Choice()]
        return _Resp()


@pytest.fixture
def fake_openai(monkeypatch):
    _FakeOpenAI.last_init_kwargs = None
    _FakeOpenAI.last_call_kwargs = None
    import openai
    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    yield _FakeOpenAI


def test_openai_compatible_requires_base_url(monkeypatch, tmp_path):
    _base_env(
        monkeypatch, tmp_path,
        LLM_PROVIDER="openai-compatible",
        LLM_MODEL="llama3.1:8b",
        OPENAI_API_KEY="",
    )
    with pytest.raises(Exception) as ei:
        cfg.get_settings()
    assert "LLM_BASE_URL" in str(ei.value)


def test_openai_compatible_passes_base_url_and_placeholder_key(monkeypatch, tmp_path, fake_openai):
    _base_env(
        monkeypatch, tmp_path,
        LLM_PROVIDER="openai-compatible",
        LLM_MODEL="llama3.1:8b",
        LLM_BASE_URL="http://host.docker.internal:11434/v1",
        OPENAI_API_KEY="",
    )
    p = classifier_mod.get_provider()
    assert p.name == "openai-compatible"
    assert p.model == "llama3.1:8b"
    init = fake_openai.last_init_kwargs
    assert init["base_url"] == "http://host.docker.internal:11434/v1"
    assert init["api_key"] == "not-needed"


def test_openai_compatible_forwards_real_api_key_when_provided(monkeypatch, tmp_path, fake_openai):
    _base_env(
        monkeypatch, tmp_path,
        LLM_PROVIDER="openai-compatible",
        LLM_MODEL="meta-llama/llama-3.1-8b-instruct",
        LLM_BASE_URL="https://openrouter.ai/api/v1",
        OPENAI_API_KEY="sk-or-real-key",
    )
    classifier_mod.get_provider()
    assert fake_openai.last_init_kwargs["api_key"] == "sk-or-real-key"


def test_response_format_json_can_be_disabled(monkeypatch, tmp_path, fake_openai):
    _base_env(
        monkeypatch, tmp_path,
        LLM_PROVIDER="openai-compatible",
        LLM_MODEL="llama3.1:8b",
        LLM_BASE_URL="http://host.docker.internal:11434/v1",
        OPENAI_API_KEY="",
        LLM_RESPONSE_FORMAT_JSON="false",
    )
    p = classifier_mod.get_provider()
    p.complete("system", "user")
    assert "response_format" not in fake_openai.last_call_kwargs


def test_response_format_json_default_on(monkeypatch, tmp_path, fake_openai):
    _base_env(
        monkeypatch, tmp_path,
        LLM_PROVIDER="openai-compatible",
        LLM_MODEL="llama3.1:8b",
        LLM_BASE_URL="http://host.docker.internal:11434/v1",
        OPENAI_API_KEY="",
    )
    p = classifier_mod.get_provider()
    p.complete("system", "user")
    assert fake_openai.last_call_kwargs["response_format"] == {"type": "json_object"}


def test_openai_proper_does_not_set_base_url(monkeypatch, tmp_path, fake_openai):
    _base_env(
        monkeypatch, tmp_path,
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="sk-real",
    )
    classifier_mod.get_provider()
    assert "base_url" not in fake_openai.last_init_kwargs
    assert fake_openai.last_init_kwargs["api_key"] == "sk-real"


def test_openai_proper_still_requires_key(monkeypatch, tmp_path):
    _base_env(
        monkeypatch, tmp_path,
        LLM_PROVIDER="openai",
        LLM_MODEL="gpt-4o-mini",
        OPENAI_API_KEY="",
    )
    with pytest.raises(Exception) as ei:
        cfg.get_settings()
    assert "OPENAI_API_KEY" in str(ei.value)
