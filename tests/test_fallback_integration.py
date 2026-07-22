"""End-to-end fault-injection (voice): with FALLBACK_ENABLED and the OSS endpoint
DEAD, moderation and core-chat streaming must fall back to the managed model.

SKIPPED unless RUN_FALLBACK_INTEGRATION=1 and a real OPENAI_API_KEY is set — makes
real managed-model calls and needs network (run in staging/CI).

    RUN_FALLBACK_INTEGRATION=1 OPENAI_API_KEY=sk-... LLM_MODEL_NAME=gpt-4.1 \\
        .venv/bin/python -m pytest tests/test_fallback_integration.py -o asyncio_mode=auto -s
"""

import os

import asyncio

import pytest

_KEY = os.getenv("OPENAI_API_KEY", "")
pytestmark = pytest.mark.skipif(
    os.getenv("RUN_FALLBACK_INTEGRATION") != "1" or not _KEY or _KEY == "test-key",
    reason="set RUN_FALLBACK_INTEGRATION=1 and a real OPENAI_API_KEY (needs network + managed model)",
)

DEAD_OSS_URL = "http://127.0.0.1:1/v1"  # port 1 -> connection refused


@pytest.fixture
def oss_dead(monkeypatch):
    from app.services import fallback as fb
    from app.llm_core.factory import MaterializedTier
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    dead = OpenAIChatModel("gemma-dead", provider=OpenAIProvider(base_url=DEAD_OSS_URL, api_key="x"))
    monkeypatch.setattr(fb.settings, "fallback_enabled", True)

    def _managed_model():
        return OpenAIChatModel(os.getenv("LLM_MODEL_NAME", "gpt-4.1"),
                               provider=OpenAIProvider(api_key=_KEY))

    # Config-driven chain (the only path post-P4): a DEAD OSS vLLM tier (connection
    # refused) first, then the real managed model — the walker must classify the
    # connection failure and fall back before commit.
    async def _resolve_chain(*, pipeline, session_id, variant):
        return [
            MaterializedTier(kind="oss", handle=dead, model_name="gemma-dead",
                             provider="vllm", endpoint=DEAD_OSS_URL, timeout=5.0),
            MaterializedTier(kind="managed", handle=_managed_model(), model_name="gpt-4.1",
                             provider="openai", endpoint="managed", timeout=20.0),
        ]

    monkeypatch.setattr(fb, "_resolve_chain", _resolve_chain)
    events = []
    monkeypatch.setattr(fb, "emit", events.append)
    return fb, events, dead


def test_unary_moderation_falls_back_to_managed(oss_dead, monkeypatch):
    fb, events, _dead = oss_dead
    from app.services import moderation as mod
    from openai import AsyncOpenAI

    dead_client = AsyncOpenAI(base_url=DEAD_OSS_URL, api_key="x")
    real_client, real_model, _ = mod._client_model_for_kind("managed")  # real OpenAI

    monkeypatch.setattr(
        mod,
        "_client_model_for_kind",
        lambda kind: (dead_client, "gemma-dead", "vllm") if kind == "oss" else (real_client, real_model, "openai"),
    )

    verdict = asyncio.run(
        mod.check_moderation("My cow has a fever, what should I do?", "english", variant="oss", session_id="it-mod")
    )
    assert verdict.category != "unavailable", "managed tier should have produced a real verdict"
    assert any(e.fell_back for e in events), "expected an OSS->managed fallback event"


def test_streaming_voice_falls_back_to_managed(oss_dead):
    fb, events, _dead = oss_dead
    from agents.voice import voice_agent
    from agents.deps import FarmerContext

    deps = FarmerContext(query="Reply with a short greeting.", session_id="it-voice")

    async def make_stream(attempt):
        async with voice_agent.run_stream(
            user_prompt="Reply with a short greeting.",
            message_history=[],
            deps=deps,
            model=attempt.model,
        ) as rs:
            async for c in rs.stream_text(delta=True, debounce_by=0):
                yield c

    async def drive():
        chunks = []
        async for c in fb.stream_with_fallback(
            pipeline="chat", session_id="it-voice", variant="oss", make_stream=make_stream
        ):
            chunks.append(c)
        return chunks

    chunks = asyncio.run(drive())
    assert "".join(chunks).strip(), "expected streamed tokens from the managed model"
    assert any(e.fell_back and not e.committed for e in events), "expected a pre-first-token OSS->managed swap"
