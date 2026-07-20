import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

# Env shim: alias pydantic-ai 1.x ``OpenAIChatModel`` to 0.2.4 ``OpenAIModel`` so
# non_meaningful (-> translation -> agents.models, which builds a model at import)
# imports under both. No model object is called (all model calls are stubbed).
import pydantic_ai.models.openai as _pai_openai  # noqa: E402
if not hasattr(_pai_openai, "OpenAIChatModel"):
    _pai_openai.OpenAIChatModel = _pai_openai.OpenAIModel

import asyncio  # noqa: E402

from app.services.non_meaningful import _parse_verdict, check_non_meaningful_streak  # noqa: E402


def test_parse_non_meaningful_valid_true():
    verdict = _parse_verdict(
        '{"five_consecutive_non_meaningful": true, "reason": "five filler turns"}'
    )
    assert verdict.five_consecutive_non_meaningful is True
    assert verdict.failed_open is False


def test_parse_non_meaningful_invalid_json_fails_open():
    verdict = _parse_verdict("not-json")
    assert verdict.five_consecutive_non_meaningful is False
    assert verdict.failed_open is True


def test_non_meaningful_less_than_five_turns_returns_false():
    verdict = asyncio.run(
        check_non_meaningful_streak(
            user_turns=["hello", "hmm", "ok"],
            source_lang="gu",
        )
    )
    assert verdict.five_consecutive_non_meaningful is False
    assert verdict.failed_open is False
