"""
Voice content moderation.

Runs an LLM classifier in parallel with query pretranslation to catch
inputs that are out-of-scope for the Amul dairy helpline — irrelevant,
offensive, culturally sensitive, or aberrant usage of the helpline.

Fail-open: on timeout, parse error, or any exception we allow the query
through (label `in_scope`). A flaky moderation call must not drop a
legitimate farmer call.
"""

import asyncio
import json
import os
from dataclasses import dataclass, replace
from typing import Literal, Optional

from openai import AsyncOpenAI

from app.config import settings
from app.services.fallback import attempt_chain, classify, execute_with_fallback
from app.services.translation import (
    OPENAI_PRETRANSLATION_MODEL,
    OSS_PRETRANSLATION_MODEL,
    _get_langfuse,
    _get_openai_client,
    _get_oss_pretranslation_client,
)
from helpers.utils import get_logger, get_prompt

logger = get_logger(__name__)


ModerationCategory = Literal[
    "in_scope",
    "irrelevant",
    "offensive",
    "cultural_sensitivity",
    "aberration",
    # Internal-only: not a model output. Marks a fail-CLOSED verdict (the
    # moderation gate could not produce a trustworthy result on any tier).
    "unavailable",
]


REJECT_CATEGORIES: frozenset[str] = frozenset(
    {"irrelevant", "offensive", "cultural_sensitivity", "aberration", "unavailable"}
)


DECLINE_MESSAGES_EN: dict[str, str] = {
    "irrelevant": (
        "This helpline answers questions about animal health, dairy, and farming. "
        "Do you have a question about your animals?"
    ),
    "offensive": (
        "This is a service for farmers. Please keep the conversation respectful, "
        "otherwise I will have to end the call."
    ),
    "cultural_sensitivity": (
        "I cannot discuss this topic. "
        "Do you have a question about your animals or farming?"
    ),
    "aberration": (
        "This helpline only handles dairy farming and animal husbandry questions. "
        "For other matters, please contact the appropriate service."
    ),
    "unavailable": (
        "I'm having trouble processing your request right now. "
        "Please try again in a moment."
    ),
}


MODERATION_PROMPT_NAME = "voice_moderation_en"
_STATIC_MODERATION_SYSTEM_PROMPT = get_prompt(MODERATION_PROMPT_NAME)

# Voice moderation runs on the self-hosted gemma vLLM endpoint by default: it is
# fast (~0.2s, measured in the load sweep) and in-cluster, vs ~1.5s for the
# OpenAI gpt path that previously served it — which doubled as the long pole on
# warm-cache voice turns. Override with VOICE_MODERATION_PROVIDER=openai to fall
# back to the gpt model (OPENAI_PRETRANSLATION_MODEL).
_MODERATION_PROVIDER = (os.getenv("VOICE_MODERATION_PROVIDER", "vllm") or "vllm").strip().lower()


def _moderation_client_and_model() -> tuple[AsyncOpenAI, str, str]:
    """Return (client, model, provider_label) for the configured moderation backend
    (legacy path: single global VOICE_MODERATION_PROVIDER)."""
    if _MODERATION_PROVIDER == "openai":
        return _get_openai_client(), OPENAI_PRETRANSLATION_MODEL, "openai"
    return _get_oss_pretranslation_client(), OSS_PRETRANSLATION_MODEL, "vllm"


def _client_model_for_kind(kind: str) -> tuple[AsyncOpenAI, str, str]:
    """Return (client, model, provider_label) for one fallback-chain attempt:
    'oss' -> self-hosted vLLM, anything else -> managed OpenAI."""
    if kind == "oss":
        return _get_oss_pretranslation_client(), OSS_PRETRANSLATION_MODEL, "vllm"
    return _get_openai_client(), OPENAI_PRETRANSLATION_MODEL, "openai"


@dataclass(frozen=True)
class ModerationVerdict:
    category: ModerationCategory
    reason: str
    raw_output: Optional[str] = None
    failed_open: bool = False
    failed_closed: bool = False
    requested_tier: Optional[str] = None
    requested_provider: Optional[str] = None
    requested_model: Optional[str] = None
    actual_tier: Optional[str] = None
    actual_provider: Optional[str] = None
    actual_model: Optional[str] = None
    fallback_used: Optional[bool] = None
    attempts: Optional[list[dict[str, object]]] = None

    @property
    def rejected(self) -> bool:
        return self.category in REJECT_CATEGORIES

    def decline_text_en(self) -> Optional[str]:
        return DECLINE_MESSAGES_EN.get(self.category)


def _allow(reason: str, *, raw_output: Optional[str] = None, failed_open: bool = False) -> ModerationVerdict:
    return ModerationVerdict(
        category="in_scope",
        reason=reason,
        raw_output=raw_output,
        failed_open=failed_open,
    )


def _block_unavailable(reason: str, *, raw_output: Optional[str] = None) -> ModerationVerdict:
    """Fail-CLOSED verdict: blocks the turn with a generic 'try again' decline
    when moderation can't produce a trustworthy result."""
    return ModerationVerdict(
        category="unavailable",
        reason=reason,
        raw_output=raw_output,
        failed_closed=True,
    )


def _with_telemetry(
    verdict: ModerationVerdict,
    *,
    requested_tier: Optional[str],
    requested_provider: Optional[str],
    requested_model: Optional[str],
    actual_tier: Optional[str],
    actual_provider: Optional[str],
    actual_model: Optional[str],
    fallback_used: Optional[bool],
    attempts: Optional[list[dict[str, object]]],
) -> ModerationVerdict:
    return replace(
        verdict,
        requested_tier=requested_tier,
        requested_provider=requested_provider,
        requested_model=requested_model,
        actual_tier=actual_tier,
        actual_provider=actual_provider,
        actual_model=actual_model,
        fallback_used=fallback_used,
        attempts=attempts,
    )


def _parse_verdict(raw: str) -> ModerationVerdict:
    """Parse the model's JSON output. Fail-open on any parsing issue."""
    stripped = (raw or "").strip()
    if not stripped:
        logger.warning("Moderation returned empty output; failing open")
        return _allow("empty model output", raw_output=raw, failed_open=True)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("Moderation returned non-JSON output; failing open - raw=%r", stripped[:200])
        return _allow("non-json model output", raw_output=raw, failed_open=True)

    if not isinstance(data, dict):
        logger.warning("Moderation returned non-object JSON; failing open - raw=%r", stripped[:200])
        return _allow("non-object model output", raw_output=raw, failed_open=True)

    category = (data.get("category") or "").strip().lower()
    reason = (data.get("reason") or "").strip()[:200]

    valid = {"in_scope", "irrelevant", "offensive", "cultural_sensitivity", "aberration"}
    if category not in valid:
        logger.warning(
            "Moderation returned unknown category=%r; failing open - raw=%r",
            category,
            stripped[:200],
        )
        return _allow(f"unknown category: {category}", raw_output=raw, failed_open=True)

    return ModerationVerdict(category=category, reason=reason, raw_output=raw)  # type: ignore[arg-type]


def _parse_verdict_strict(raw: str) -> ModerationVerdict:
    """Like _parse_verdict but fails CLOSED on malformed/untrustworthy output
    (returns an `unavailable` reject) instead of failing open. Valid verdicts —
    including reject categories — are returned unchanged. Used on the
    fallback-enabled path so a garbage response blocks rather than waves through."""
    stripped = (raw or "").strip()
    if not stripped:
        logger.warning("Moderation returned empty output; failing closed")
        return _block_unavailable("empty model output", raw_output=raw)

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        logger.warning("Moderation returned non-JSON output; failing closed - raw=%r", stripped[:200])
        return _block_unavailable("non-json model output", raw_output=raw)

    if not isinstance(data, dict):
        logger.warning("Moderation returned non-object JSON; failing closed - raw=%r", stripped[:200])
        return _block_unavailable("non-object model output", raw_output=raw)

    category = (data.get("category") or "").strip().lower()
    reason = (data.get("reason") or "").strip()[:200]

    valid = {"in_scope", "irrelevant", "offensive", "cultural_sensitivity", "aberration"}
    if category not in valid:
        logger.warning("Moderation returned unknown category=%r; failing closed - raw=%r", category, stripped[:200])
        return _block_unavailable(f"unknown category: {category}", raw_output=raw)

    return ModerationVerdict(category=category, reason=reason, raw_output=raw)  # type: ignore[arg-type]


def _build_messages(
    text: str,
    source_lang: str,
    recent_history_text: str = "",
) -> list[dict[str, str]]:
    user_parts = [f"Source language: {source_lang}"]
    if recent_history_text.strip():
        user_parts.append(f"Recent conversation context:\n{recent_history_text.strip()}")
    user_parts.append(f"Caller utterance:\n{text.strip()}")
    user_content = "\n\n".join(user_parts)
    return [
        {"role": "system", "content": _STATIC_MODERATION_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]


async def _create_moderation_response(
    client: AsyncOpenAI,
    model: str,
    text: str,
    source_lang: str,
    recent_history_text: str = "",
):
    return await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=_build_messages(text, source_lang, recent_history_text),
            max_completion_tokens=200,
            response_format={"type": "json_object"},
        ),
        timeout=settings.openai_pretranslation_timeout_seconds,
    )


async def check_moderation(
    text: str,
    source_lang: str,
    recent_history_text: str = "",
    variant: str = "legacy",
    session_id: str = "",
    user_id: str = "",
    process_id: str = "",
    pipeline_variant: str = "",
) -> ModerationVerdict:
    """Classify a caller utterance. Returns a ModerationVerdict.

    With ``settings.fallback_enabled`` (standard path): route by the session
    *variant* (OSS for OSS sessions, managed for legacy) through the OSS->managed
    fallback chain, and **fail CLOSED** — return an ``unavailable`` reject when no
    tier produces a trustworthy verdict. Mirrors amul-oan-api moderation. Failing
    closed only triggers when both OSS and managed fail, so it does not drop calls
    on a single-provider blip.

    Without it (legacy path): a single global provider
    (``VOICE_MODERATION_PROVIDER``) and **fail OPEN** — today's behaviour, so the
    kill-switch reverts exactly.
    """
    if not text or not text.strip():
        return _with_telemetry(
            _allow("empty input", failed_open=False),
            requested_tier="none",
            requested_provider="none",
            requested_model="none",
            actual_tier="none",
            actual_provider="none",
            actual_model="none",
            fallback_used=False,
            attempts=[],
        )

    if not settings.fallback_enabled:
        return await _check_moderation_legacy(
            text,
            source_lang,
            recent_history_text,
            session_id=session_id,
            user_id=user_id,
            process_id=process_id,
            pipeline_variant=pipeline_variant,
        )

    chain = attempt_chain(variant, "moderation")
    requested = chain[0]
    _, requested_model, requested_provider = _client_model_for_kind(requested.kind)
    attempts: list[dict[str, object]] = []
    actual_tier = requested.kind
    actual_provider = requested_provider
    actual_model = requested_model

    async def _run(attempt):
        nonlocal actual_tier, actual_provider, actual_model
        client, model, provider = _client_model_for_kind(attempt.kind)
        attempt_info: dict[str, object] = {
            "tier": attempt.kind,
            "provider": provider,
            "model": model,
            "endpoint": attempt.endpoint,
            "status": "started",
        }
        attempts.append(attempt_info)
        try:
            response = await _create_moderation_response(client, model, text, source_lang, recent_history_text)
            raw = (response.choices[0].message.content or "").strip()
            verdict = _parse_verdict_strict(raw)
            attempt_info["status"] = "ok"
            actual_tier = attempt.kind
            actual_provider = provider
            actual_model = model
            return verdict
        except Exception as exc:
            attempt_info["status"] = "error"
            attempt_info["error_class"] = type(exc).__name__
            attempt_info["error_reason"] = classify(exc).value
            raise

    try:
        verdict = await execute_with_fallback(
            pipeline="moderation",
            session_id=session_id or "",
            variant=variant,
            run=_run,
        )
        fallback_used = len(attempts) > 1 and attempts[0].get("status") == "error"
        return _with_telemetry(
            verdict,
            requested_tier=requested.kind,
            requested_provider=requested_provider,
            requested_model=requested_model,
            actual_tier=actual_tier,
            actual_provider=actual_provider,
            actual_model=actual_model,
            fallback_used=fallback_used,
            attempts=attempts,
        )
    except Exception as e:
        logger.error(
            "Moderation failed on all tiers; failing closed - source_lang=%s error=%s",
            source_lang,
            type(e).__name__,
        )
        fallback_used = len(attempts) > 1 and attempts[0].get("status") == "error"
        return _with_telemetry(
            _block_unavailable(f"moderation unavailable: {type(e).__name__}"),
            requested_tier=requested.kind,
            requested_provider=requested_provider,
            requested_model=requested_model,
            actual_tier="failed",
            actual_provider="failed",
            actual_model="failed",
            fallback_used=fallback_used,
            attempts=attempts,
        )


async def _check_moderation_legacy(
    text: str,
    source_lang: str,
    recent_history_text: str = "",
    *,
    session_id: str = "",
    user_id: str = "",
    process_id: str = "",
    pipeline_variant: str = "",
) -> ModerationVerdict:
    """Legacy moderation: single global provider (VOICE_MODERATION_PROVIDER),
    fails OPEN. Used when ``settings.fallback_enabled`` is false."""
    requested_tier = "oss" if _MODERATION_PROVIDER != "openai" else "managed"
    requested_provider = "vllm" if requested_tier == "oss" else "openai"
    requested_model = OSS_PRETRANSLATION_MODEL if requested_tier == "oss" else OPENAI_PRETRANSLATION_MODEL
    attempts: list[dict[str, object]] = [
        {
            "tier": requested_tier,
            "provider": requested_provider,
            "model": requested_model,
            "status": "started",
        }
    ]
    try:
        client, model, provider = _moderation_client_and_model()
        requested_tier = "managed" if provider == "openai" else "oss"
        attempts[0]["tier"] = requested_tier
        requested_provider = provider
        requested_model = model
        attempts[0]["provider"] = requested_provider
        attempts[0]["model"] = requested_model
    except Exception as e:
        logger.error("Moderation client init failed (%s); failing open", e)
        attempts[0]["status"] = "error"
        attempts[0]["error_class"] = type(e).__name__
        attempts[0]["error_reason"] = classify(e).value
        return _with_telemetry(
            _allow(f"moderation client error: {type(e).__name__}", failed_open=True),
            requested_tier=requested_tier,
            requested_provider=requested_provider,
            requested_model=requested_model,
            actual_tier="failed",
            actual_provider="failed",
            actual_model="failed",
            fallback_used=False,
            attempts=attempts,
        )
    langfuse = _get_langfuse()

    try:
        if not langfuse:
            response = await _create_moderation_response(
                client,
                model,
                text,
                source_lang,
                recent_history_text,
            )
            raw = (response.choices[0].message.content or "").strip()
            verdict = _parse_verdict(raw)
            attempts[0]["status"] = "ok"
            return _with_telemetry(
                verdict,
                requested_tier=requested_tier,
                requested_provider=requested_provider,
                requested_model=requested_model,
                actual_tier=requested_tier,
                actual_provider=requested_provider,
                actual_model=requested_model,
                fallback_used=False,
                attempts=attempts,
            )

        with langfuse.start_as_current_observation(
            name="query_moderation",
            as_type="generation",
            input={
                "source_lang": source_lang,
                "text": text,
                "recent_history_text": recent_history_text,
            },
            model=model,
            metadata={
                "pipeline_stage": "query_moderation",
                "moderation_provider": provider,
            },
        ) as observation:
            response = await _create_moderation_response(
                client,
                model,
                text,
                source_lang,
                recent_history_text,
            )
            raw = (response.choices[0].message.content or "").strip()
            verdict = _parse_verdict(raw)
            attempts[0]["status"] = "ok"
            observation.update(
                output={
                    "category": verdict.category,
                    "reason": verdict.reason,
                    "failed_open": verdict.failed_open,
                },
                metadata={"rejected": verdict.rejected},
            )
            return _with_telemetry(
                verdict,
                requested_tier=requested_tier,
                requested_provider=requested_provider,
                requested_model=requested_model,
                actual_tier=requested_tier,
                actual_provider=requested_provider,
                actual_model=requested_model,
                fallback_used=False,
                attempts=attempts,
            )
    except asyncio.TimeoutError:
        logger.error(
            "Moderation timed out - source_lang=%s model=%s timeout=%.2fs query_chars=%s query_preview=%r",
            source_lang,
            model,
            settings.openai_pretranslation_timeout_seconds,
            len(text),
            text[:160],
        )
        attempts[0]["status"] = "error"
        attempts[0]["error_class"] = "TimeoutError"
        attempts[0]["error_reason"] = "timeout"
        return _with_telemetry(
            _allow("moderation timeout", failed_open=True),
            requested_tier=requested_tier,
            requested_provider=requested_provider,
            requested_model=requested_model,
            actual_tier="failed",
            actual_provider="failed",
            actual_model="failed",
            fallback_used=False,
            attempts=attempts,
        )
    except Exception as e:
        logger.error(
            "Moderation failed - source_lang=%s error=%s query_preview=%r",
            source_lang,
            e,
            text[:160],
        )
        attempts[0]["status"] = "error"
        attempts[0]["error_class"] = type(e).__name__
        attempts[0]["error_reason"] = classify(e).value
        return _with_telemetry(
            _allow(f"moderation error: {type(e).__name__}", failed_open=True),
            requested_tier=requested_tier,
            requested_provider=requested_provider,
            requested_model=requested_model,
            actual_tier="failed",
            actual_provider="failed",
            actual_model="failed",
            fallback_used=False,
            attempts=attempts,
        )
