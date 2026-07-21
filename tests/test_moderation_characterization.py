"""Characterization test pinning voice moderation's *effective* behaviour across
the P4 dual-moderation collapse.

The P4 change collapses:
  * ``_parse_verdict`` (fail-OPEN) + ``_parse_verdict_strict`` (fail-CLOSED) into
    one ``_parse_verdict(raw, *, fail_closed)`` (the strict name kept as an alias);
  * the two client-getter code paths into resolver-only helpers.

The collapse is DECIDED to be behaviour-preserving: same input -> same terminal
outcome per condition as the pre-change tree. This module pins today's outcomes so
the de-duplication cannot silently change *which condition blocks vs allows*.

The pinned values below were captured from the PRE-change code (both parsers +
the fail-open legacy error path + the fail-closed fallback-all-tiers-fail path)
and must remain green through the collapse.

NOTE (env): moderation transitively imports ``agents.models`` (constructs a model
at import) which uses ``OpenAIChatModel`` — present in the pinned pydantic-ai 1.x,
absent in the locally installed 0.2.4. We alias the name so this module imports +
runs under both; no model object is ever called (every model call is stubbed).
"""

import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

import pydantic_ai.models.openai as _pai_openai  # noqa: E402
if not hasattr(_pai_openai, "OpenAIChatModel"):
    _pai_openai.OpenAIChatModel = _pai_openai.OpenAIModel

import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from app.services import fallback as fb  # noqa: E402
from app.services import moderation as mod  # noqa: E402


def _resp(content: str):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))])


# Inputs spanning every classification branch of the parser.
_MALFORMED = ["", "not json", '"a string"', '{"category": "weird"}']
_VALID_IN_SCOPE = '{"category": "in_scope", "reason": "ok"}'
_VALID_REJECT = '{"category": "offensive", "reason": "x"}'
_VALID_OTHER = '{"category": "irrelevant"}'


# ── (1) parser verdicts: fail-open (non-strict) ───────────────────────────────

@pytest.mark.parametrize("raw", _MALFORMED)
def test_parse_open_malformed_allows(raw):
    """Fail-OPEN: malformed/unknown output -> in_scope, failed_open=True (today's
    FALLBACK_ENABLED-off behaviour). Pinned from the pre-change _parse_verdict."""
    v = mod._parse_verdict(raw)
    assert v.category == "in_scope"
    assert v.failed_open is True
    assert v.failed_closed is False
    assert v.rejected is False


def test_parse_open_valid_in_scope_unchanged():
    v = mod._parse_verdict(_VALID_IN_SCOPE)
    assert v.category == "in_scope" and v.failed_open is False and not v.rejected


def test_parse_open_valid_reject_unchanged():
    v = mod._parse_verdict(_VALID_REJECT)
    assert v.category == "offensive" and v.failed_open is False and v.rejected


def test_parse_open_valid_other_category_unchanged():
    v = mod._parse_verdict(_VALID_OTHER)
    assert v.category == "irrelevant" and v.failed_open is False and v.rejected


# ── (2) parser verdicts: fail-closed (strict) ─────────────────────────────────

@pytest.mark.parametrize("raw", _MALFORMED)
def test_parse_strict_malformed_blocks(raw):
    """Fail-CLOSED: malformed/unknown output -> unavailable reject,
    failed_closed=True (today's fallback-path behaviour). Pinned from the
    pre-change _parse_verdict_strict; also reachable via _parse_verdict(fail_closed=True)."""
    for v in (mod._parse_verdict_strict(raw), mod._parse_verdict(raw, fail_closed=True)):
        assert v.category == "unavailable"
        assert v.failed_closed is True
        assert v.failed_open is False
        assert v.rejected is True


def test_parse_strict_valid_verdicts_unchanged():
    """A VALID verdict (incl. a reject) is returned unchanged under BOTH policies —
    the strict/non-strict parsers only diverge on malformed input."""
    assert mod._parse_verdict_strict(_VALID_IN_SCOPE).category == "in_scope"
    assert not mod._parse_verdict_strict(_VALID_IN_SCOPE).failed_closed
    assert mod._parse_verdict_strict(_VALID_REJECT).category == "offensive"
    assert mod._parse_verdict_strict(_VALID_REJECT).rejected


def test_strict_alias_equivalent_to_parametrized():
    """``_parse_verdict_strict(x)`` == ``_parse_verdict(x, fail_closed=True)`` on
    the terminal category/flags for every pinned input (the alias is a pure
    re-expression, not a second implementation)."""
    for raw in _MALFORMED + [_VALID_IN_SCOPE, _VALID_REJECT, _VALID_OTHER]:
        a = mod._parse_verdict_strict(raw)
        b = mod._parse_verdict(raw, fail_closed=True)
        assert (a.category, a.failed_open, a.failed_closed) == (b.category, b.failed_open, b.failed_closed)


# ── (3) fail-OPEN legacy path result on error (FALLBACK_ENABLED off) ──────────

def test_legacy_path_fails_open_on_client_error(monkeypatch):
    """Today's behaviour: FALLBACK_ENABLED off -> the single-provider legacy path;
    a moderation-call error ALLOWS the turn (in_scope, failed_open) and never
    blocks. Do NOT change which condition blocks vs allows."""
    monkeypatch.setattr(mod.settings, "fallback_enabled", False)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: None)
    monkeypatch.setattr(mod, "_moderation_client_and_model", lambda: ("client", "model", "openai"))

    async def _boom(client, model, text, source_lang, recent_history_text=""):
        raise ConnectionError("moderation backend down")

    monkeypatch.setattr(mod, "_create_moderation_response", _boom)

    v = asyncio.run(mod.check_moderation("hi", "gu", variant="oss", session_id="s"))
    assert v.category == "in_scope"
    assert v.failed_open is True
    assert v.rejected is False


def test_legacy_path_malformed_output_fails_open(monkeypatch):
    """Legacy path + malformed model output -> fail OPEN (non-strict parser)."""
    monkeypatch.setattr(mod.settings, "fallback_enabled", False)
    monkeypatch.setattr(mod, "_get_langfuse", lambda: None)
    monkeypatch.setattr(mod, "_moderation_client_and_model", lambda: ("client", "model", "openai"))

    async def _garbage(client, model, text, source_lang, recent_history_text=""):
        return _resp("not-json-at-all")

    monkeypatch.setattr(mod, "_create_moderation_response", _garbage)

    v = asyncio.run(mod.check_moderation("hi", "gu", variant="legacy", session_id="s"))
    assert v.category == "in_scope" and v.failed_open is True and not v.rejected


# ── (4) fail-CLOSED fallback path result on error (FALLBACK_ENABLED on) ───────

@pytest.fixture
def _fallback_on(monkeypatch):
    """FALLBACK_ENABLED on with a controlled [oss, managed] chain + per-kind
    backends, capturing emitted events (mirrors test_voice_moderation_fallback)."""
    monkeypatch.setattr(mod.settings, "fallback_enabled", True)

    async def _chain(*, pipeline, session_id, variant):
        from app.llm_core.factory import MaterializedTier
        if variant == "oss":
            return [
                MaterializedTier(kind="oss", handle=object(), model_name="gemma",
                                 provider="vllm", endpoint="http://oss:8020/v1", timeout=None),
                MaterializedTier(kind="managed", handle=object(), model_name="gpt",
                                 provider="openai", endpoint="managed", timeout=None),
            ]
        return [MaterializedTier(kind="managed", handle=object(), model_name="gpt",
                                 provider="openai", endpoint="managed", timeout=None)]

    monkeypatch.setattr(fb, "_resolve_chain", _chain)
    monkeypatch.setattr(fb, "emit", lambda e: None)
    monkeypatch.setattr(mod, "_client_model_for_kind",
                        lambda kind: (f"{kind}-c", f"{kind}-m", "vllm" if kind == "oss" else "openai"))


def test_fallback_path_all_tiers_fail_blocks_closed(_fallback_on, monkeypatch):
    """Today's behaviour: FALLBACK_ENABLED on + BOTH tiers error -> fail CLOSED
    (unavailable reject, failed_closed). This is the whole point of the fallback
    path and must not change."""
    async def _boom(client, model, text, source_lang, recent_history_text=""):
        raise ConnectionError("down")

    monkeypatch.setattr(mod, "_create_moderation_response", _boom)

    v = asyncio.run(mod.check_moderation("hi", "gu", variant="oss", session_id="s"))
    assert v.category == "unavailable"
    assert v.failed_closed is True
    assert v.rejected is True
    assert v.actual_tier == "failed"


def test_fallback_path_valid_reject_blocks_without_fallback(_fallback_on, monkeypatch):
    """A VALID reject on the first (oss) tier blocks immediately — no fallback,
    identical category to today."""
    async def _reject(client, model, text, source_lang, recent_history_text=""):
        return _resp(_VALID_REJECT)

    monkeypatch.setattr(mod, "_create_moderation_response", _reject)

    v = asyncio.run(mod.check_moderation("x", "gu", variant="oss", session_id="s"))
    assert v.category == "offensive" and v.rejected and v.fallback_used is False
    assert v.actual_tier == "oss"


def test_fallback_path_malformed_output_blocks_closed(_fallback_on, monkeypatch):
    """Fallback path + malformed output on every tier -> fail CLOSED (strict
    parser blocks rather than waving through)."""
    async def _garbage(client, model, text, source_lang, recent_history_text=""):
        return _resp("garbage")

    monkeypatch.setattr(mod, "_create_moderation_response", _garbage)

    v = asyncio.run(mod.check_moderation("x", "gu", variant="oss", session_id="s"))
    assert v.category == "unavailable" and v.failed_closed and v.rejected


# ── (5) empty input short-circuit is allow, both policies ─────────────────────

def test_empty_input_allows_regardless_of_flag(monkeypatch):
    for enabled in (False, True):
        monkeypatch.setattr(mod.settings, "fallback_enabled", enabled)
        v = asyncio.run(mod.check_moderation("   ", "gu", variant="oss", session_id="s"))
        assert v.category == "in_scope" and not v.rejected
