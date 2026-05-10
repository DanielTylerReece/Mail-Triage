"""Tier 2 classifier — provider-agnostic. Supports Anthropic and OpenAI.

Valid categories and their descriptions are loaded from categories.txt at
startup — adding a new category does not require code changes here.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Protocol

from bs4 import BeautifulSoup

from . import categories as cats
from .config import get_settings

log = logging.getLogger(__name__)


@dataclass
class Tier2Result:
    category: str
    confidence: float
    reasoning: str
    raw_output: str
    error: str | None = None
    model: str | None = None
    provider: str | None = None


# -------------------- Provider protocol --------------------


class Provider(Protocol):
    name: str
    model: str

    def complete(self, system_prompt: str, user_payload: str) -> str: ...


# -------------------- Anthropic --------------------


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, api_key: str, model: str, timeout: int) -> None:
        from anthropic import Anthropic
        self.model = model
        self._client = Anthropic(api_key=api_key, timeout=timeout)

    def complete(self, system_prompt: str, user_payload: str) -> str:
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=256,
            system=[{
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }],
            messages=[{"role": "user", "content": user_payload}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")


# -------------------- OpenAI --------------------


class OpenAIProvider:
    name = "openai"

    def __init__(self, api_key: str, model: str, timeout: int) -> None:
        from openai import OpenAI
        self.model = model
        self._client = OpenAI(api_key=api_key, timeout=timeout)

    def complete(self, system_prompt: str, user_payload: str) -> str:
        resp = self._client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_payload},
            ],
            max_tokens=256,
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""


# -------------------- Factory --------------------


_provider: Provider | None = None


def _secret(value: Any) -> str:
    """Coerce SecretStr or plain str to a plain string."""
    if hasattr(value, "get_secret_value"):
        return value.get_secret_value()
    return str(value or "")


def get_provider() -> Provider:
    global _provider
    if _provider is not None:
        return _provider
    s = get_settings()
    if s.llm_provider == "anthropic":
        key = _secret(s.anthropic_api_key)
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set but LLM_PROVIDER=anthropic")
        _provider = AnthropicProvider(key, s.llm_model, s.llm_timeout_seconds)
    elif s.llm_provider == "openai":
        key = _secret(s.openai_api_key)
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set but LLM_PROVIDER=openai")
        _provider = OpenAIProvider(key, s.llm_model, s.llm_timeout_seconds)
    else:
        raise RuntimeError(f"unknown LLM_PROVIDER: {s.llm_provider}")
    log.info("Tier 2 classifier: provider=%s model=%s", _provider.name, _provider.model)
    return _provider


def reset_provider_for_tests() -> None:
    global _provider
    _provider = None


# -------------------- System prompt rendering --------------------


_PLACEHOLDER = "{categories_block}"
_cached_system_prompt: str | None = None


def system_prompt() -> str:
    global _cached_system_prompt
    if _cached_system_prompt is not None:
        return _cached_system_prompt
    s = get_settings()
    template = s.classifier_prompt_file.read_text(encoding="utf-8-sig")
    block = cats.render_for_prompt(cats.load())
    if _PLACEHOLDER in template:
        rendered = template.replace(_PLACEHOLDER, block)
    else:
        rendered = template + "\n\n" + block
    _cached_system_prompt = rendered
    return rendered


def reset_prompt_cache_for_tests() -> None:
    global _cached_system_prompt
    _cached_system_prompt = None


# -------------------- Public API --------------------


def classify(msg: dict[str, Any], *, mailbox: str | None = None,
             message_id: str | None = None) -> Tier2Result:
    """Classify a single email. Falls back to 'triage' on any error.
    `mailbox` and `message_id` are optional context for log lines."""
    payload = _build_payload(msg)
    try:
        prompt = system_prompt()
        provider = get_provider()
    except Exception as e:
        return Tier2Result("triage", 0.0, "init failed", "",
                           error=type(e).__name__)

    try:
        raw = provider.complete(prompt, payload)
    except Exception as e:
        log.exception("provider %s call failed for %s/%s",
                      provider.name, mailbox or "?", message_id or "?")
        return Tier2Result(
            "triage", 0.0, f"{provider.name} call failed", "",
            error=type(e).__name__, provider=provider.name, model=provider.model,
        )

    result = _parse_output(raw)
    result.provider = provider.name
    result.model = provider.model
    return result


# -------------------- Payload + parser --------------------


_MAX_NAME_LEN = 100
_MAX_ADDR_LEN = 254
_MAX_SUBJECT_LEN = 500
_MAX_TO_LIST_LEN = 500
_MAX_RECIPIENTS = 10

_OPEN_TAG = "<untrusted_email>"
_CLOSE_TAG = "</untrusted_email>"

# Tighter wrapper-defang regex: matches the literal tags AND tolerant
# variations like `</untrusted email>` or `< / untrusted_email >` that
# sneak through a plain-string defang. Case-insensitive. Prevents
# sibling-tag injection attempts in any header or body field.
_TAG_DEFANG_RE = re.compile(
    r"<\s*/?\s*untrusted[\s_\-]*email\s*>",
    re.IGNORECASE,
)


def _build_payload(msg: dict[str, Any]) -> str:
    from_obj = (msg.get("from") or {}).get("emailAddress") or {}
    sender_addr = _sanitize_header(from_obj.get("address") or "unknown", _MAX_ADDR_LEN)
    sender_name = _sanitize_header(from_obj.get("name") or "", _MAX_NAME_LEN)

    raw_recipients = msg.get("toRecipients") or []
    sanitized_recipients = [
        _sanitize_header((r.get("emailAddress") or {}).get("address", ""), _MAX_ADDR_LEN)
        for r in raw_recipients[:_MAX_RECIPIENTS]
    ]
    sanitized_recipients = [r for r in sanitized_recipients if r]
    to_addrs = ", ".join(sanitized_recipients)
    if len(raw_recipients) > _MAX_RECIPIENTS:
        to_addrs += f" (+{len(raw_recipients) - _MAX_RECIPIENTS} more)"
    if len(to_addrs) > _MAX_TO_LIST_LEN:
        to_addrs = to_addrs[:_MAX_TO_LIST_LEN] + " [...]"

    subject = _sanitize_header(msg.get("subject") or "", _MAX_SUBJECT_LEN)

    body = _extract_body(msg)
    body = _sanitize_body(body)
    if len(body) > 2048:
        body = body[:2048] + "\n[... truncated ...]"

    return (
        f"{_OPEN_TAG}\n"
        f"From: {sender_name} <{sender_addr}>\n"
        f"To: {to_addrs}\n"
        f"Subject: {subject}\n\n"
        f"{body}\n"
        f"{_CLOSE_TAG}"
    )


def _extract_body(msg: dict[str, Any]) -> str:
    body = msg.get("body") or {}
    content = body.get("content") or msg.get("bodyPreview") or ""
    if (body.get("contentType") or "").lower() == "html":
        try:
            content = BeautifulSoup(content, "lxml").get_text(separator="\n")
        except Exception:
            pass
    return content


def _sanitize_header(text: str, max_len: int) -> str:
    if text is None:
        return ""
    s = str(text).strip()
    s = _strip_lone_surrogates(s)
    s = unicodedata.normalize("NFKC", s)
    # Replace ALL control / format / line-separator chars with a single
    # space. Includes Cc, Cf, Zl (U+2028), Zp (U+2029) — many tokenizers
    # treat Zl/Zp as line breaks.
    s = "".join(
        " " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c
        for c in s
    )
    s = _TAG_DEFANG_RE.sub("[redacted-tag]", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 7] + " [...]"
    return s


def _sanitize_body(text: str) -> str:
    if text is None:
        return ""
    s = _strip_lone_surrogates(str(text))
    s = unicodedata.normalize("NFKC", s)
    # Preserve normal whitespace (\n \r \t) but strip everything else in
    # Cc / Cf / Zl / Zp. Zl (U+2028) and Zp (U+2029) are NOT in \s by
    # default for some tokenizers and used to be a sibling-tag injection
    # vector; strip them outright in body content.
    out: list[str] = []
    for c in s:
        cat = unicodedata.category(c)
        if cat in ("Cc",) and c not in "\n\r\t":
            continue
        if cat in ("Cf", "Zl", "Zp"):
            continue
        out.append(c)
    s = "".join(out)
    s = _TAG_DEFANG_RE.sub("[redacted-tag]", s)
    return s


def _strip_lone_surrogates(s: str) -> str:
    """Lone surrogates (Cs) cannot be encoded as UTF-8 and would crash any
    downstream SQLite TEXT insertion. Replace them with U+FFFD."""
    return "".join("�" if 0xD800 <= ord(c) <= 0xDFFF else c for c in s)


_sanitize = _sanitize_body  # back-compat alias


def _parse_output(raw: str) -> Tier2Result:
    """Extract and validate a JSON object from the model's output.

    Strategy: walk forward through the text trying `json.JSONDecoder.
    raw_decode()` from each `{` candidate. We prefer the LAST successfully
    parsed object — if the model echoed an attacker-injected JSON early
    in its output and then wrote its own real answer, the model's wins.
    Schema validation against the categories enum still constrains the
    worst case to a fixed set."""
    text = raw.strip()
    if not text:
        return Tier2Result("triage", 0.0, "empty output", text, error="empty")

    decoder = json.JSONDecoder()
    obj = None
    pos = 0
    while True:
        idx = text.find("{", pos)
        if idx == -1:
            break
        try:
            candidate, end = decoder.raw_decode(text[idx:])
            if isinstance(candidate, dict):
                obj = candidate  # keep walking; last parseable wins
                pos = idx + end
                continue
        except json.JSONDecodeError:
            pass
        pos = idx + 1

    if obj is None:
        return Tier2Result("triage", 0.0, "no parseable JSON in output", text, error="no_json")

    category = str(obj.get("category", "")).lower().strip()
    valid = cats.valid_names()
    if category not in valid:
        return Tier2Result("triage", 0.0, f"invalid category {category!r}", text, error="bad_category")

    raw_conf = obj.get("confidence", 0.0)
    try:
        confidence = float(raw_conf)
    except (TypeError, ValueError):
        confidence = 0.0
    if confidence > 1.5 or confidence < 0:
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    reasoning = str(obj.get("reasoning", "")).strip()[:100]
    return Tier2Result(category, confidence, reasoning, text)
