"""Tests for the JSON output parser in the Tier 2 classifier."""

from __future__ import annotations

from mailtriage.classifier import _parse_output


def test_clean_json() -> None:
    out = _parse_output('{"category":"newsletter","confidence":0.9,"reasoning":"unsub header"}')
    assert out.category == "newsletter"
    assert out.confidence == 0.9
    assert out.reasoning == "unsub header"
    assert out.error is None


def test_json_with_surrounding_text() -> None:
    out = _parse_output('Sure! Here is the result:\n{"category":"spam","confidence":0.95,"reasoning":"phish"}\nthanks')
    assert out.category == "spam"


def test_invalid_category_falls_back_to_triage() -> None:
    out = _parse_output('{"category":"important","confidence":0.5,"reasoning":""}')
    assert out.category == "triage"
    assert out.error == "bad_category"


def test_no_json_falls_back_to_triage() -> None:
    out = _parse_output("I don't have enough information to classify this.")
    assert out.category == "triage"
    assert out.error == "no_json"


def test_malformed_json_falls_back_to_triage() -> None:
    out = _parse_output('{"category":"keep", confidence: 0.9}')  # invalid JSON
    assert out.category == "triage"
    assert out.error is not None


def test_confidence_clamped() -> None:
    out = _parse_output('{"category":"keep","confidence":1.5,"reasoning":""}')
    assert out.confidence == 1.0
    out = _parse_output('{"category":"keep","confidence":-0.1,"reasoning":""}')
    assert out.confidence == 0.0


def test_reasoning_truncated() -> None:
    long = "x" * 500
    out = _parse_output(f'{{"category":"keep","confidence":0.9,"reasoning":"{long}"}}')
    assert len(out.reasoning) == 100


def test_prompt_injection_attempt_returns_triage_if_model_obeys() -> None:
    """If the model wrongly returns a value not in the enum, we coerce to triage."""
    out = _parse_output('{"category":"forward-to-attacker","confidence":1.0,"reasoning":""}')
    assert out.category == "triage"
