"""
Translation service using TranslateGemma models.

Provides translation between Indian languages and English using
TranslateGemma 27B base model deployed on vLLM.
"""

import os
import json
import re
import time
import aiohttp
import asyncio
import anyio
from typing import Literal, Optional, Sequence
from openai import AsyncOpenAI
from helpers.utils import get_logger, normalize_voice_output
from dotenv import load_dotenv
from agents.tools.terms import get_mini_glossary_for_text, get_ambiguity_hints_for_query, TERM_PAIRS
from app.services.translation_gujarati import (
    GU_PREFERRED_TRANSLATION_RULES,
    GU_POST_REPLACEMENTS,
    GU_TERM_POLICY,
    _StreamingGujaratiTermNormalizer,
    _apply_protected_output,
    _buffered_protected_stream,
    _fix_dandas,
    _flush_streaming_translation,
    _normalize_streaming_translation_chunk,
    _post_normalize_gu_translation,
    _protected_output_triggers,
)
from app.config import settings

# Post-translation now flows through the unified llm_core config chain
# (Step.POST_TRANSLATION = [TranslateGemma(LB), managed-LLM overflow]). These are
# the seams the adapter walks; the disconnect-safe first-token primitive
# (with_first_token_deadline) and the failure classifier are reused verbatim — the
# translation logic (incl. voice's per-chunk normalize_voice_output + rstrip decode)
# is preserved, only the tier walk + cross-provider overflow are new.
from app.llm_core import resolver as _llm_resolver
from app.llm_core import health as _llm_health
from app.llm_core import trace as _llm_trace
from app import metrics as _metrics
from app.llm_core.config_model import (
    Step as _Step,
    Tier as _Tier,
    Provider as _Provider,
    StepClientKind as _StepClientKind,
)
from app.llm_core.factory import TGDescriptor as _TGDescriptor, build_handle as _build_handle
from app.services.fallback import (
    FALLBACKABLE as _FALLBACKABLE,
    FallbackEvent as _FallbackEvent,
    classify as _classify,
    emit as _emit,
    with_first_token_deadline as _with_first_token_deadline,
)

try:
    from langfuse import get_client as get_langfuse_client
except ImportError:
    get_langfuse_client = None

load_dotenv()

logger = get_logger(__name__)


class _TranslationHTTPError(Exception):
    """A non-200 from the TranslateGemma endpoint, carrying ``status_code`` so the
    shared ``classify`` sees the real HTTP status (HTTP_5XX / RATE_LIMITED / OOM)
    instead of collapsing every failure to UNKNOWN. ``classify`` reads
    ``exc.status_code`` first, so exposing it here restores honest fallback-reason
    telemetry and 4xx-vs-5xx slicing across the post-translation chain."""

    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        super().__init__(f"Translation failed with status {status}: {body}")


# Pretranslation provider — follows main LLM_PROVIDER by default.
# Override with PRETRANSLATION_PROVIDER if you want a different provider for pretranslation.
# Supported: "openai" | "vllm" (OpenAI-compatible endpoint, e.g. local Gemma 4 via vLLM).
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
PRETRANSLATION_PROVIDER = os.getenv("PRETRANSLATION_PROVIDER", LLM_PROVIDER).lower()
if PRETRANSLATION_PROVIDER == "vllm":
    _PRETRANSLATION_MODEL_DEFAULT = os.getenv("LLM_MODEL_NAME", "gemma-4-31b-it")
else:
    _PRETRANSLATION_MODEL_DEFAULT = "gpt-5.1"
# OPENAI_PRETRANSLATION_MODEL kept as legacy alias; PRETRANSLATION_MODEL takes precedence.
OPENAI_PRETRANSLATION_MODEL = os.getenv(
    "PRETRANSLATION_MODEL",
    os.getenv("OPENAI_PRETRANSLATION_MODEL", _PRETRANSLATION_MODEL_DEFAULT),
)
_openai_client: Optional[AsyncOpenAI] = None

# OSS pretranslation (vLLM) — used per-request only for sticky 'oss' sessions,
# independent of the startup PRETRANSLATION_PROVIDER so legacy sessions are
# completely unaffected. Mirrors amul-oan-api's OSS pretranslation path.
OSS_INFERENCE_ENDPOINT_URL = (os.getenv("OSS_INFERENCE_ENDPOINT_URL", "") or "").rstrip("/")
OSS_INFERENCE_API_KEY = os.getenv("OSS_INFERENCE_API_KEY") or "dummy"
OSS_PRETRANSLATION_MODEL = os.getenv(
    "OSS_PRETRANSLATION_MODEL", os.getenv("OSS_LLM_MODEL_NAME", "gemma-4-31b-it")
)
_oss_pretrans_client: Optional[AsyncOpenAI] = None


def _get_oss_pretranslation_client() -> AsyncOpenAI:
    """OpenAI-compatible client pinned to the OSS vLLM endpoint."""
    global _oss_pretrans_client
    if _oss_pretrans_client is None:
        if not OSS_INFERENCE_ENDPOINT_URL:
            raise ValueError(
                "OSS_INFERENCE_ENDPOINT_URL is required for OSS pretranslation"
            )
        _oss_pretrans_client = AsyncOpenAI(
            api_key=OSS_INFERENCE_API_KEY, base_url=OSS_INFERENCE_ENDPOINT_URL
        )
    return _oss_pretrans_client




# Only the 27b-base TranslateGemma is deployed, BEHIND AN NGINX LB — the SINGULAR
# TRANSLATEGEMMA_27B_BASE_ENDPOINT IS that LB (it fans out to replicas server-side),
# so there is exactly one client-facing endpoint (no client-side endpoint list /
# random.choice anymore). Post-translation model+endpoint selection now flows
# through the llm_core config chain (Step.POST_TRANSLATION); these singular
# constants also back the voice pretranslation structured fallback, which still
# speaks TranslateGemma ``/completions`` directly.
TRANSLATEGEMMA_27B_BASE_ENDPOINT = os.getenv("TRANSLATEGEMMA_27B_BASE_ENDPOINT", "http://localhost:18002/v1")
TRANSLATEGEMMA_27B_BASE_MODEL = os.getenv("TRANSLATEGEMMA_27B_BASE_MODEL", "translategemma-27b-base")

LANG_NAMES = {
    "marathi": "Marathi", "english": "English", "hindi": "Hindi",
    "gujarati": "Gujarati", "tamil": "Tamil", "kannada": "Kannada",
    "odia": "Oriya", "telugu": "Telugu", "punjabi": "Punjabi",
    "malayalam": "Malayalam", "bengali": "Bengali", "urdu": "Urdu",
    "assamese": "Assamese",
    "mr": "Marathi", "en": "English", "hi": "Hindi", "gu": "Gujarati",
    "ta": "Tamil", "kn": "Kannada", "or": "Oriya", "te": "Telugu",
    "pa": "Punjabi", "ml": "Malayalam", "bn": "Bengali", "ur": "Urdu",
    "as": "Assamese"
}

LANG_CODES = {
    "marathi": "mr", "english": "en", "hindi": "hi", "gujarati": "gu",
    "tamil": "ta", "kannada": "kn", "odia": "or", "telugu": "te",
    "punjabi": "pa", "malayalam": "ml", "bengali": "bn", "urdu": "ur",
    "assamese": "as",
    "mr": "mr", "en": "en", "hi": "hi", "gu": "gu", "ta": "ta",
    "kn": "kn", "or": "or", "te": "te", "pa": "pa", "ml": "ml",
    "bn": "bn", "ur": "ur", "as": "as"
}

INDIAN_LANGUAGES = [
    "marathi", "mr", "hindi", "hi", "gujarati", "gu", "tamil", "ta",
    "kannada", "kn", "odia", "or", "telugu", "te", "punjabi", "pa",
    "malayalam", "ml", "bengali", "bn", "urdu", "ur", "assamese", "as"
]


def _build_translation_instruction(
    text: str,
    source_lang: str,
    target_lang: str,
    mini_glossary: Optional[str] = None,
) -> str:
    """Build the translation INSTRUCTION text (voice spoken-language preamble +
    glossary Rules + voice GU style rules) — the same wording used for BOTH tiers:
    TranslateGemma consumes it wrapped in the Gemma chat template
    (:func:`_wrap_translategemma_prompt`), while the cross-provider LLM overflow tier
    consumes it verbatim as the chat.completions user message. Kept byte-identical to
    the prior inline instruction (voice's spoken-language preamble, no length rule) so
    the two paths are indistinguishable in prompt."""
    source_name = LANG_NAMES.get(source_lang.lower(), source_lang.capitalize())
    target_name = LANG_NAMES.get(target_lang.lower(), target_lang.capitalize())
    source_code = LANG_CODES.get(source_lang.lower(), source_lang.lower())
    target_code = LANG_CODES.get(target_lang.lower(), target_lang.lower())

    instruction = (
        f"You are a professional {source_name} ({source_code}) to {target_name} ({target_code}) translator. "
        f"Your goal is to accurately convey the meaning and nuances of the original {source_name} text "
        f"while adhering to {target_name} grammar, vocabulary, and cultural sensitivities.\n"
        f"Produce only the {target_name} translation, without any additional explanations or commentary.\n"
        f"Prefer clear spoken language over literal formatting. Do not preserve markdown, bullets, numbered lists, or bracketed duplicates if they hurt voice clarity."
    )
    if mini_glossary and mini_glossary.strip():
        lines = mini_glossary.strip().splitlines()
        rules = []
        for line in lines:
            if " -> " in line:
                en_term, gu_term = line.split(" -> ", 1)
                rules.append(f"Rule: '{en_term.strip()}' must be translated as '{gu_term.strip()}'.")
        if rules:
            instruction += "\n\n**Terminology Rules (mandatory):**\n" + "\n".join(rules) + "\n"
    if target_code == "gu":
        instruction += (
            "\n\n**Gujarati Livestock Style Rules (mandatory):**\n- "
            + "\n- ".join(GU_PREFERRED_TRANSLATION_RULES)
            + "\n"
        )
    instruction += f"\n\nPlease translate the following {source_name} text into {target_name}:\n\n\n{text.strip()}"
    return instruction


def _wrap_translategemma_prompt(instruction: str) -> str:
    """Wrap an instruction in TranslateGemma's official chat template."""
    return (
        f"<bos><start_of_turn>user\n"
        f"{instruction}<end_of_turn>\n"
        f"<start_of_turn>model\n"
    )


def _format_translation_prompt(
    text: str,
    source_lang: str,
    target_lang: str,
    mini_glossary: Optional[str] = None,
) -> str:
    """Format the TranslateGemma text-completion prompt (instruction + chat template).
    When target is Gujarati and mini_glossary is provided, injects a dynamic term list
    so the model uses consistent domain terminology."""
    return _wrap_translategemma_prompt(
        _build_translation_instruction(
            text, source_lang, target_lang, mini_glossary=mini_glossary,
        )
    )


def _get_langfuse():
    if not get_langfuse_client:
        return None
    try:
        return get_langfuse_client()
    except Exception:
        return None


def _is_untranslatable_fragment(text: str) -> bool:
    """True when there is nothing to translate — the fragment has no letters or
    digits in any script (pure punctuation / markdown / symbols, e.g. ``**``).

    TranslateGemma free-generates an unrelated canned paragraph when handed such a
    degenerate fragment (a streaming chunk boundary can isolate ``**`` on its own),
    so we must short-circuit and return it verbatim instead of calling the model.
    This matters MORE for voice than chat: voice drives ``translate_text_stream_fast``
    per streaming chunk, so a lone ``**`` chunk is exactly the degenerate-chunk case
    behind the TG "appended-garbage" hallucination whose prescribed fix is this
    pre-model chunk guard (ported verbatim from chat)."""
    return not re.search(r"[^\W_]", text, flags=re.UNICODE)


# ── post-translation tier-chain adapter ───────────────────────────────────────
# translate_text / translate_text_stream_fast route through the llm_core
# POST_TRANSLATION chain: [TranslateGemma(LB), managed-LLM overflow]. Per tier the
# handle is a TGDescriptor (aiohttp text-completion) or an AsyncOpenAI client
# (chat.completions). The SAME instruction (voice spoken-language preamble + glossary
# Rules + voice GU style rules, no length rule) and the SAME stream-safe transform
# pipeline (danda fix -> incremental term replacement -> remaining Gujarati cleanup ->
# ``normalize_voice_output(streaming=True)`` for streams;
# ``_fix_dandas -> _post_normalize_gu_translation -> normalize_voice_output`` for the
# unary path) apply to BOTH tiers. First-token-commit + classify-based overflow mirror
# ``stream_with_fallback``; the disconnect-safe TTFT primitive
# (``with_first_token_deadline``) is reused verbatim. Post-translation is
# profile-invariant (llm_core ``defaults``), so the chain is variant-independent — we
# resolve with "legacy". A dedicated walker (not ``stream_with_fallback`` /
# ``execute_with_fallback``) is used because those resolve their chain internally from
# a pipeline-string + session_id + weighted split, which post-translation has none of.
_POST_TRANSLATION_PIPELINE = "posttranslation"


def _prepare_translation_inputs(text, source_lang, target_lang):
    """Mini-glossary fetch + build the translation instruction ONCE (shared by both
    tiers) + the Gemma-wrapped TranslateGemma prompt. Verbatim to the prior inline
    logic (mini glossary for gu at threshold 0.90 / max 40). Voice has no
    max_output_chars length rule."""
    mini_glossary = ""
    if target_lang.lower() in ("gujarati", "gu"):
        mini_glossary = get_mini_glossary_for_text(text, threshold=0.90, max_terms=40)
        if mini_glossary:
            logger.info(f"Translation prompt: injected mini glossary ({len(mini_glossary.splitlines())} terms)")
    instruction = _build_translation_instruction(
        text, source_lang, target_lang, mini_glossary=mini_glossary,
    )
    tg_prompt = _wrap_translategemma_prompt(instruction)
    return instruction, tg_prompt


def _is_translategemma_tier(tier) -> bool:
    """A tier whose handle speaks TranslateGemma ``/completions`` over aiohttp.
    Decided from the provider label alone so it never forces a lazy handle build."""
    return getattr(tier, "provider", "") == "translategemma"


class _PostTranslationTier:
    """One entry in the post-translation fallback chain. Carries the telemetry
    metadata the walkers read (``kind``/``provider``/``model_name``/``endpoint``/
    ``timeout``) but builds its client handle LAZILY, memoized, on first access.

    TranslateGemma serves ~every turn and its ``TGDescriptor`` primary is a cheap,
    provider-independent URL holder; the managed-LLM overflow is an ``AsyncOpenAI``
    client that is expensive to construct and can legitimately fail to build in a
    healthy-TG env. Building lazily means the overflow client is constructed ONLY
    when a request actually falls through to it — never eagerly per call, and a
    misconfigured overflow tier can never take a healthy primary down (its build
    error, if reached, is just this tier's failure and hits the caller degrade net,
    exactly like the old pure-TG path)."""

    __slots__ = ("_tier", "kind", "model_name", "endpoint", "timeout", "ttft", "_memo")

    def __init__(self, tier: _Tier):
        self._tier = tier
        self.kind = "oss" if tier.provider is _Provider.VLLM else "managed"
        self.model_name = tier.model
        self.endpoint = tier.endpoint or "managed"
        self.timeout = (tier.timeout_ms / 1000.0) if tier.timeout_ms is not None else None
        # Distinct SHORT first-token deadline (seconds) — the stream walker bounds
        # time-to-first-token by this, keeping ``timeout`` as the overall/total cap.
        # ``None`` (e.g. the managed overflow tier) -> walker falls back to ``timeout``.
        self.ttft = (tier.ttft_ms / 1000.0) if tier.ttft_ms is not None else None
        self._memo: list = []

    @property
    def provider(self) -> str:
        return self._tier.provider.value

    @property
    def handle(self):
        if not self._memo:
            kind = (
                _StepClientKind.TRANSLATEGEMMA
                if self._tier.provider is _Provider.TRANSLATEGEMMA
                else _StepClientKind.RAW_OPENAI
            )
            self._memo.append(_build_handle(self._tier, kind))
        return self._memo[0]


class _TtftDeadlineView:
    """Minimal view exposing ONLY the attributes ``with_first_token_deadline`` reads
    (``timeout``/``kind``/``endpoint``), so a post-translation tier can present a
    SHORT first-token bound (its ``ttft``) to that primitive while its real
    ``timeout`` stays the 60s overall/total cap the unary walker + aiohttp honor.
    A saturated-but-ALIVE TranslateGemma thus overflows in single-digit seconds
    instead of blocking a voice turn for up to the full 60s."""

    __slots__ = ("timeout", "kind", "endpoint")

    def __init__(self, tier, ttft):
        self.timeout = ttft
        self.kind = tier.kind
        self.endpoint = tier.endpoint


def _record_served_tier(tier, index: int) -> None:
    """Record the tier that actually served this post-translation turn: onto the
    per-turn Langfuse pipeline trace (served kind + 0-based chain index) and the
    Prometheus served counter (the managed-share KPI). Both hooks are best-effort
    no-ops off-path (no active trace context / prometheus_client absent)."""
    _llm_trace.record_served(_Step.POST_TRANSLATION, tier.kind, index)
    _metrics.record_served(
        _Step.POST_TRANSLATION.value, tier.kind, tier.provider, tier.model_name
    )


def _post_translation_chain():
    """Resolve the INERT POST_TRANSLATION tiers, HEALTH-PRUNE known-down endpoints,
    wrap the survivors as lazy-handle chain entries + record the trace chain.
    Post-translation is profile-invariant (llm_core ``defaults``), so we resolve
    with ``"legacy"``.

    The prune is why this exists: the post-translation walkers DO feed the breaker
    (``record_failure``/``record_success``) and the poller polls the TG ``/health``,
    but without pruning here a down TG is re-attempted (and re-timed-out) EVERY turn
    on the most-traveled path. ``prune_unhealthy`` drops tiers whose endpoint breaker
    is ``open`` and is contractually never-empty (all-open -> chain returned
    unchanged), and it is a settings-gated identity no-op when the health flags are
    off, so the flags-off path is byte-identical."""
    tiers = _llm_resolver.post_translation_tiers()  # profile-invariant (lives in defaults)
    tiers = _llm_health.prune_unhealthy(_Step.POST_TRANSLATION, tiers)
    chain = [_PostTranslationTier(t) for t in tiers]
    _llm_trace.record_step_chain(_Step.POST_TRANSLATION, chain)
    return chain


async def _translategemma_stream(descriptor, prompt, source_lang, target_lang, text, temperature, max_tokens):
    """VERBATIM TranslateGemma streaming SSE decode (aiohttp), incl. the
    ``stream_translation`` Langfuse observation and its ``if not langfuse:`` branch.
    Voice chunks share the incremental terminology pipeline used by the managed
    fallback; line decode remains via ``.rstrip('\\r')``."""
    translated_parts: list[str] = []
    langfuse = _get_langfuse()
    normalizer = _StreamingGujaratiTermNormalizer(target_lang)

    if not langfuse:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                descriptor.completions_url,
                json={
                    "model": descriptor.model_id,
                    "prompt": prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Translation API error {response.status}: {error_text}")
                    raise _TranslationHTTPError(response.status, error_text)

                buffer = b''
                async for chunk in response.content.iter_chunked(64):
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        line = line.decode('utf-8').rstrip('\r')
                        if line.startswith('data: '):
                            data = line[6:]
                            if data == '[DONE]':
                                break
                            try:
                                chunk_data = json.loads(data)
                                content = chunk_data['choices'][0].get('text', '')
                                if content:
                                    content = _normalize_streaming_translation_chunk(
                                        normalizer, content, target_lang,
                                    )
                                    if content:
                                        translated_parts.append(content)
                                        yield content
                            except json.JSONDecodeError:
                                continue
        tail = _flush_streaming_translation(normalizer, target_lang)
        if tail:
            translated_parts.append(tail)
            yield tail
        return

    with langfuse.start_as_current_observation(
        name="stream_translation",
        as_type="generation",
        input={
            "source_lang": source_lang,
            "target_lang": target_lang,
            "text": text,
        },
        model=descriptor.model_id,
        metadata={
            "translation_provider": "translategemma",
            "model_size": "27b-base",
            "stream": "true",
            "pipeline_stage": "stream_translation",
        },
    ) as observation:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                descriptor.completions_url,
                json={
                    "model": descriptor.model_id,
                    "prompt": prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Translation API error {response.status}: {error_text}")
                    raise _TranslationHTTPError(response.status, error_text)

                buffer = b''
                async for chunk in response.content.iter_chunked(64):
                    buffer += chunk
                    while b'\n' in buffer:
                        line, buffer = buffer.split(b'\n', 1)
                        line = line.decode('utf-8').rstrip('\r')
                        if line.startswith('data: '):
                            data = line[6:]
                            if data == '[DONE]':
                                break
                            try:
                                chunk_data = json.loads(data)
                                content = chunk_data['choices'][0].get('text', '')
                                if content:
                                    content = _normalize_streaming_translation_chunk(
                                        normalizer, content, target_lang,
                                    )
                                    if content:
                                        translated_parts.append(content)
                                        yield content
                            except json.JSONDecodeError:
                                continue
        tail = _flush_streaming_translation(normalizer, target_lang)
        if tail:
            translated_parts.append(tail)
            yield tail
        observation.update(output="".join(translated_parts))


async def _llm_translation_stream(client, model_name, instruction, source_lang, target_lang, text, temperature, max_tokens):
    """Cross-provider overflow: a managed chat LLM does en->target translation with
    the SAME instruction and incremental terminology pipeline as TranslateGemma.
    Mirrors the TG observation for parity."""
    langfuse = _get_langfuse()
    normalizer = _StreamingGujaratiTermNormalizer(target_lang)

    if not langfuse:
        stream = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": instruction}],
            temperature=temperature,
            max_completion_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            content = getattr(chunk.choices[0].delta, "content", None) or ""
            if content:
                content = _normalize_streaming_translation_chunk(
                    normalizer, content, target_lang,
                )
                if content:
                    yield content
        tail = _flush_streaming_translation(normalizer, target_lang)
        if tail:
            yield tail
        return

    translated_parts: list[str] = []
    with langfuse.start_as_current_observation(
        name="stream_translation",
        as_type="generation",
        input={
            "source_lang": source_lang,
            "target_lang": target_lang,
            "text": text,
        },
        model=model_name,
        metadata={
            "translation_provider": "llm-fallback",
            "stream": "true",
            "pipeline_stage": "stream_translation",
        },
    ) as observation:
        stream = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": instruction}],
            temperature=temperature,
            max_completion_tokens=max_tokens,
            stream=True,
        )
        async for chunk in stream:
            if not getattr(chunk, "choices", None):
                continue
            content = getattr(chunk.choices[0].delta, "content", None) or ""
            if content:
                content = _normalize_streaming_translation_chunk(
                    normalizer, content, target_lang,
                )
                if content:
                    translated_parts.append(content)
                    yield content
        tail = _flush_streaming_translation(normalizer, target_lang)
        if tail:
            translated_parts.append(tail)
            yield tail
        observation.update(output="".join(translated_parts))


async def _translategemma_unary(descriptor, prompt, source_lang, target_lang, text, temperature, max_tokens):
    """VERBATIM non-stream TranslateGemma call (reads full body ``choices[0].text``)
    incl. the ``text_translation`` Langfuse observation and its ``if not langfuse:``
    branch. Voice per-response transforms: ``_fix_dandas ->
    _post_normalize_gu_translation -> normalize_voice_output`` (unconditional)."""
    langfuse = _get_langfuse()

    if not langfuse:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                descriptor.completions_url,
                json={
                    "model": descriptor.model_id,
                    "prompt": prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Translation API error {response.status}: {error_text}")
                    raise _TranslationHTTPError(response.status, error_text)

                result = await response.json()
                translated_text = result["choices"][0]["text"].strip()
                translated_text = _fix_dandas(translated_text)
                translated_text = _post_normalize_gu_translation(translated_text, target_lang)
                translated_text = normalize_voice_output(translated_text, target_lang)
                logger.info(f"Translation successful ({len(text)} -> {len(translated_text)} chars)")
                return translated_text

    with langfuse.start_as_current_observation(
        name="text_translation",
        as_type="generation",
        input={
            "source_lang": source_lang,
            "target_lang": target_lang,
            "text": text,
        },
        model=descriptor.model_id,
        metadata={
            "translation_provider": "translategemma",
            "model_size": "27b-base",
            "pipeline_stage": "text_translation",
        },
    ) as observation:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                descriptor.completions_url,
                json={
                    "model": descriptor.model_id,
                    "prompt": prompt,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                timeout=aiohttp.ClientTimeout(total=60),
            ) as response:
                if response.status != 200:
                    error_text = await response.text()
                    logger.error(f"Translation API error {response.status}: {error_text}")
                    raise _TranslationHTTPError(response.status, error_text)

                result = await response.json()
                translated_text = result["choices"][0]["text"].strip()
                translated_text = _fix_dandas(translated_text)
                translated_text = _post_normalize_gu_translation(translated_text, target_lang)
                translated_text = normalize_voice_output(translated_text, target_lang)
                observation.update(output=translated_text)
                logger.info(f"Translation successful ({len(text)} -> {len(translated_text)} chars)")
                return translated_text


async def _llm_translation_unary(client, model_name, instruction, source_lang, target_lang, text, temperature, max_tokens):
    """Cross-provider overflow (non-stream): chat.completions with the SAME
    instruction, reading ``choices[0].message.content`` and applying the SAME voice
    transforms (``_fix_dandas -> _post_normalize_gu_translation ->
    normalize_voice_output`` unconditional)."""
    langfuse = _get_langfuse()

    if not langfuse:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": instruction}],
            temperature=temperature,
            max_completion_tokens=max_tokens,
        )
        translated_text = (response.choices[0].message.content or "").strip()
        translated_text = _fix_dandas(translated_text)
        translated_text = _post_normalize_gu_translation(translated_text, target_lang)
        translated_text = normalize_voice_output(translated_text, target_lang)
        logger.info(f"Translation successful ({len(text)} -> {len(translated_text)} chars)")
        return translated_text

    with langfuse.start_as_current_observation(
        name="text_translation",
        as_type="generation",
        input={
            "source_lang": source_lang,
            "target_lang": target_lang,
            "text": text,
        },
        model=model_name,
        metadata={
            "translation_provider": "llm-fallback",
            "pipeline_stage": "text_translation",
        },
    ) as observation:
        response = await client.chat.completions.create(
            model=model_name,
            messages=[{"role": "user", "content": instruction}],
            temperature=temperature,
            max_completion_tokens=max_tokens,
        )
        translated_text = (response.choices[0].message.content or "").strip()
        translated_text = _fix_dandas(translated_text)
        translated_text = _post_normalize_gu_translation(translated_text, target_lang)
        translated_text = normalize_voice_output(translated_text, target_lang)
        observation.update(output=translated_text)
        logger.info(f"Translation successful ({len(text)} -> {len(translated_text)} chars)")
        return translated_text


async def _stream_post_translation_chain(chain, make_stream, *, source_lang, target_lang):
    """First-token-commit walker over the POST_TRANSLATION chain — mirrors
    ``fallback.stream_with_fallback`` (commit on first chunk; pre-commit fallbackable
    failure swaps to the next tier; post-commit failure propagates) but drives a
    PRE-RESOLVED chain and calls ``with_first_token_deadline`` for the disconnect-safe
    TTFT bound. Cross-provider overflow (translategemma -> managed LLM) is just the
    next tier."""
    last_exc: Optional[BaseException] = None
    for i, tier in enumerate(chain):
        t0 = time.monotonic()
        committed = False
        try:
            # Bound the FIRST-token wait by the tier's short ``ttft`` (falling back to
            # its ``timeout`` when unset); the 60s ``timeout`` remains the overall/total
            # cap (aiohttp total + unary walker), so a saturated-but-alive TG overflows
            # fast instead of holding a voice turn for the whole 60s.
            _ttft = getattr(tier, "ttft", None)
            _deadline_view = _TtftDeadlineView(
                tier, _ttft if _ttft is not None else tier.timeout
            )
            async for chunk in _with_first_token_deadline(_deadline_view, make_stream(tier)):
                committed = True
                yield chunk
            _llm_health.record_success(tier.endpoint)
            _record_served_tier(tier, i)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = _classify(exc)
            is_last = i == len(chain) - 1
            if committed:
                _emit(_FallbackEvent(
                    pipeline=_POST_TRANSLATION_PIPELINE, session_id="-",
                    from_variant=tier.kind, to_variant=None, reason=reason,
                    error_class=type(exc).__name__, error_detail=str(exc)[:500],
                    oss_endpoint=tier.endpoint, oss_model=tier.model_name,
                    latency_ms=int((time.monotonic() - t0) * 1000),
                    fell_back=False, committed=True,
                ))
                raise
            will_fall_back = reason in _FALLBACKABLE and not is_last
            if reason in _FALLBACKABLE:
                _llm_health.record_failure(tier.endpoint)
            _emit(_FallbackEvent(
                pipeline=_POST_TRANSLATION_PIPELINE, session_id="-",
                from_variant=tier.kind,
                to_variant=chain[i + 1].kind if will_fall_back else None,
                reason=reason, error_class=type(exc).__name__, error_detail=str(exc)[:500],
                oss_endpoint=tier.endpoint, oss_model=tier.model_name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                fell_back=will_fall_back, committed=False,
            ))
            last_exc = exc
            if will_fall_back:
                continue
            raise
    if last_exc is not None:
        raise last_exc


async def _run_post_translation_chain(chain, run):
    """Unary walker over the POST_TRANSLATION chain — mirrors
    ``fallback.execute_with_fallback`` (per-tier timeout via ``anyio.fail_after``;
    fall back on a classified infrastructure failure) over a PRE-RESOLVED chain."""
    last_exc: Optional[BaseException] = None
    for i, tier in enumerate(chain):
        t0 = time.monotonic()
        try:
            if tier.timeout is None:
                result = await run(tier)
            else:
                with anyio.fail_after(tier.timeout):
                    result = await run(tier)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            reason = _classify(exc)
            is_last = i == len(chain) - 1
            will_fall_back = reason in _FALLBACKABLE and not is_last
            if reason in _FALLBACKABLE:
                _llm_health.record_failure(tier.endpoint)
            _emit(_FallbackEvent(
                pipeline=_POST_TRANSLATION_PIPELINE, session_id="-",
                from_variant=tier.kind,
                to_variant=chain[i + 1].kind if will_fall_back else None,
                reason=reason, error_class=type(exc).__name__, error_detail=str(exc)[:500],
                oss_endpoint=tier.endpoint, oss_model=tier.model_name,
                latency_ms=int((time.monotonic() - t0) * 1000),
                fell_back=will_fall_back, committed=False,
            ))
            last_exc = exc
            if not will_fall_back:
                raise
        else:
            _llm_health.record_success(tier.endpoint)
            _record_served_tier(tier, i)
            return result
    if last_exc is not None:
        raise last_exc


async def translate_text(
    text: str,
    source_lang: str,
    target_lang: str,
    model_size: Optional[Literal["4b", "12b", "27b", "27b-base"]] = None,
    temperature: float = 0.0,
    max_tokens: int = 2048
) -> str:
    """Translate text via the post-translation tier chain.

    Chain = [TranslateGemma(LB), managed-LLM overflow]. Guards / prompt / per-response
    transforms are byte-identical to the prior TranslateGemma-only path (incl. the
    unconditional ``normalize_voice_output`` after post-normalize); the only additions
    are the cross-provider overflow (chat.completions with the SAME instruction) when
    TranslateGemma fails, and config-driven endpoint/model selection (no more
    client-side ``random.choice``)."""
    if not text or not text.strip():
        return text

    if _is_untranslatable_fragment(text):
        return text

    if source_lang.lower() == target_lang.lower():
        logger.info("Source and target languages are the same, skipping translation")
        return text

    _prot = _protected_output_triggers(text, target_lang)

    instruction, tg_prompt = _prepare_translation_inputs(text, source_lang, target_lang)
    logger.info(f"Translating {source_lang} -> {target_lang} via post-translation chain")

    chain = _post_translation_chain()

    async def _run(tier):
        if _is_translategemma_tier(tier):
            return await _translategemma_unary(
                tier.handle, tg_prompt, source_lang, target_lang, text, temperature, max_tokens
            )
        return await _llm_translation_unary(
            tier.handle, tier.model_name, instruction, source_lang, target_lang, text, temperature, max_tokens
        )

    result = await _run_post_translation_chain(chain, _run)
    return _apply_protected_output(result, _prot)


def _get_openai_client() -> AsyncOpenAI:
    """Return an OpenAI-compatible async client.

    When PRETRANSLATION_PROVIDER=vllm, point the OpenAI client at the local
    vLLM `INFERENCE_ENDPOINT_URL` (e.g. http://10.185.25.198:8020/v1) so the
    same chat-completions call path serves an OSS model like Gemma 4 31B IT.
    """
    global _openai_client
    if _openai_client is None:
        if PRETRANSLATION_PROVIDER == "vllm":
            base_url = os.getenv("INFERENCE_ENDPOINT_URL", "").rstrip("/")
            if not base_url:
                raise ValueError(
                    "INFERENCE_ENDPOINT_URL is required when PRETRANSLATION_PROVIDER=vllm"
                )
            api_key = os.getenv("INFERENCE_API_KEY") or "dummy"
            _openai_client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        else:
            api_key = os.getenv("OPENAI_API_KEY")
            if not api_key:
                raise ValueError("OPENAI_API_KEY is required for OpenAI pretranslation")
            _openai_client = AsyncOpenAI(api_key=api_key)
    return _openai_client


def _get_glossary_hints_for_gu_query(text: str, max_results: int = 7) -> str:
    """Fuzzy-match Gujarati input against glossary gu/transliteration fields.

    Returns a compact hint string like:
      આફરો = Bloat (rumen tympany)
      આંચળ = Udder / Teat
    """
    from rapidfuzz import fuzz as _fuzz

    if not text or not text.strip():
        return ""

    text_lower = text.lower().strip()
    scored: list[tuple[str, str, float]] = []

    for tp in TERM_PAIRS:
        scores: list[tuple[str, float]] = []
        gu_candidates = [tp.gu, *tp.gu_input_aliases]
        transliteration_candidates = [
            tp.transliteration,
            *tp.transliteration_input_aliases,
        ]

        # Check substring containment first (fast path), ignoring empty fields.
        for candidate in [*gu_candidates, *transliteration_candidates]:
            candidate_lower = (candidate or "").lower().strip()
            if candidate_lower:
                scores.append(
                    (
                        candidate,
                        100.0
                        if candidate_lower in text_lower
                        else _fuzz.partial_ratio(candidate_lower, text_lower),
                    )
                )
        if not scores:
            continue
        matched_input, best = max(scores, key=lambda item: item[1])

        if best >= 75:
            scored.append((matched_input, tp.en, best))

    if not scored:
        return ""

    # Deduplicate by English term, keep highest score
    seen_en: dict[str, tuple[str, str, float]] = {}
    for gu, en, score in scored:
        en_key = en.lower()
        if en_key not in seen_en or score > seen_en[en_key][2]:
            seen_en[en_key] = (gu, en, score)

    top = sorted(seen_en.values(), key=lambda x: x[2], reverse=True)[:max_results]
    return "\n".join(f"  {gu} = {en}" for gu, en, _ in top)


def _whole_ascii_token_pattern(term: str) -> str:
    escaped = re.escape(term.strip())
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    return rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])"


def _apply_exact_glossary_transliteration_replacements(source_text: str, translation: str) -> str:
    """Replace known transliterations when the source contains a matching input form."""
    if not source_text or not translation:
        return translation

    source_lower = source_text.lower()
    cleaned = translation

    for tp in TERM_PAIRS:
        english_label = (tp.en or "").strip()
        source_forms = [
            tp.gu,
            *tp.gu_input_aliases,
            tp.transliteration,
            *tp.transliteration_input_aliases,
        ]
        transliterations = [tp.transliteration, *tp.transliteration_input_aliases]
        if not english_label:
            continue
        if not any(
            form and form.strip().lower() in source_lower
            for form in source_forms
        ):
            continue
        if re.search(_whole_ascii_token_pattern(english_label), cleaned, flags=re.IGNORECASE):
            continue

        for transliteration in sorted(set(transliterations), key=len, reverse=True):
            transliteration = (transliteration or "").strip()
            if len(transliteration) < 3 or transliteration.lower() == english_label.lower():
                continue
            cleaned = re.sub(
                _whole_ascii_token_pattern(transliteration),
                english_label,
                cleaned,
                flags=re.IGNORECASE,
            )

    return cleaned


# ગેસ is a real veterinary term (bloat), so the species corruption table below
# is the one thing that stays conditional — applied on a turn that did not ask
# the species, it would turn "my cow has gas" into "cow".
_SPECIES_QUESTION_RE = re.compile(r"cow\s+or\s+(a\s+)?buffalo|buffalo\s+or\s+(a\s+)?cow", re.IGNORECASE)


def _conversation_context(conversation: Optional[Sequence[tuple[str, str]]]) -> str:
    """Give the translator the recent, understood conversation.

    Built by ``voice._pretranslation_context``: garbled exchanges and "please
    repeat" turns are already gone, so the last assistant turn here is the
    question the farmer is still answering even when a repeat request came in
    between. That gap is what sank session 601f1db4: "cow or buffalo?", a
    garbled answer, "please repeat", "I could not understand" — and the one-turn
    context quoted the last of those, so પસ (buffalo) became "pus" and the agent
    searched for pus treatment.

    The earlier turns resolve what one turn cannot: an animal named three turns
    back, the service the farmer already asked for, a technician list the agent
    read out before a confirmation. The answer-slot rule still points at the
    last assistant turn only — that is where the named options live.
    """
    turns = [
        (speaker, (text or "").strip())
        for speaker, text in (conversation or [])
        if (text or "").strip()
    ]
    if not turns:
        return ""
    last_assistant = next(
        (text for speaker, text in reversed(turns) if speaker == "Assistant"), None
    )
    transcript = "\n".join(f"{speaker}: {text}" for speaker, text in turns)
    block = (
        "\nConversation so far (English; turns that were not understood are omitted):\n"
        f"{transcript}\n"
        "Use it to resolve what the farmer is referring to — an animal, a service, a name "
        "already mentioned — but translate ONLY the new message; never copy earlier turns "
        "into it, and never add a meaning the new message does not carry.\n"
    )
    if last_assistant:
        block += (
            f'The assistant\'s last question was: "{last_assistant}"\n'
            "The message you are translating is most likely the farmer's ANSWER to it.\n"
            "Context rule: if that turn offered a specific set of NAMED options — two species, a "
            "list of technician names — and this message is a garbled token in the answer "
            "slot, translate it as the option it clearly corresponds to, "
            "spelled EXACTLY as that option appears above. Gujarati proper nouns survive ASR "
            "badly: syllables split, double, or drop (BAHECHARBHAI -> 'be be char bhai'), and "
            "callers say the parts out of order or give only one. If the token fits TWO of the "
            "options, or none, do NOT choose — transliterate it and leave it ambiguous, because "
            "acting on the wrong option is worse than re-asking. If the turn offered no options, "
            "this rule adds nothing: translate conservatively as the rules above require.\n"
            "Agreement is never inferred. If that question asked for a yes/no confirmation, "
            "render an affirmation or a refusal ONLY when the utterance actually carries one "
            "(હા -> yes, ના -> no). Never supply a 'yes' the farmer did not say, never drop a "
            "'ના'/'no' that they did, and if they answered with something else entirely — a "
            "question, a symptom, a name — translate THAT and leave the confirmation unanswered.\n"
        )
    if last_assistant and _SPECIES_QUESTION_RE.search(last_assistant):
        # Issue #306: 525 real answer turns, 46.1% -> 79.6% usable species, 0 regressions.
        block += (
            "Species hint: a token that plausibly sounds like ગાય (cow) or ભેંસ (buffalo) is "
            "that species. Common ASR corruptions: ગેસ/ગસ/ગ્યાસ/કેસ/ગાડી -> cow; "
            "બસ/બેસ/મેસ/બેંસ/પસ/દેસ -> buffalo. This narrows the general 'do not infer animal "
            "species' rule for THIS turn only; if it matches neither, still say 'unclear animal'.\n"
        )
    return block


def _build_openai_pretranslation_messages(
    source_name: str,
    source_code: str,
    text: str,
    conversation: Optional[Sequence[tuple[str, str]]] = None,
) -> list[dict[str, str]]:
    # -- Domain context ------------------------------------------------
    domain_preamble = (
        "You are translating messages from Indian dairy farmers calling the Amul AI helpline (voiced as 'Sarlaben' / સરલાબેન). "
        "The farmers speak Gujarati and ask about animal health, milk production, fodder, breeding, and dairy cooperative services.\n\n"
        "IMPORTANT translation rules:\n"
        "- Your job is faithful pretranslation for safe routing, not correction, completion, or advice.\n"
        "- Preserve uncertainty from the original speech. Do not repair missing words, fill missing slots, or choose a clean interpretation when the audio transcript is ambiguous.\n"
        "- Words that look like human names (e.g. સલાદ, સરલા, ગંગા) are almost always ANIMAL NAMES (cow/buffalo names). Transliterate them as-is, do NOT translate literally.\n"
        "- If a garbled token does not clearly map to a real medicine, feed, symptom, or service term, do NOT invent a meaning. Keep the translation conservative.\n"
        "- Kinship words like બેન, બહેન, ભાઈ are often address markers for Sarlaben or filler in phone speech. Do not turn them into the caller's gender. Use 'Sarlaben' only if the caller is clearly addressing the assistant; otherwise omit the address marker.\n"
        "- 'ભાઈ' in livestock context may refer to a male animal (bull/ox); keep it generic if the word could also be an address marker.\n"
        "- Prefer veterinary/agricultural meanings only when the term is clear in the original transcript. If choosing the agricultural meaning requires guessing, preserve the uncertain token.\n"
        "- Do not infer animal species. If cow/buffalo/sheep/goat is unclear, write 'unclear animal' or keep the uncertain token.\n"
    )

    domain_preamble += _conversation_context(conversation)

    # -- Ambiguity hints from ambiguity_terms.json ---------------------
    # include_ask=False so "ask" type entries (clarifying-question rules
    # meant for the answering agent) don't leak into the translator prompt
    # and get echoed back as appended follow-up questions.
    ambiguity_hints = get_ambiguity_hints_for_query(text, include_ask=False)
    if ambiguity_hints:
        domain_preamble += f"\nDomain-specific disambiguation rules for terms in this message:\n{ambiguity_hints}\n"

    # -- Glossary hints (top matching gu→en terms) ---------------------
    glossary_hints = _get_glossary_hints_for_gu_query(text, max_results=7)
    if glossary_hints:
        domain_preamble += (
            f"\nGlossary (Gujarati → English) for terms likely in this message:\n{glossary_hints}\n"
            "Glossary usage rule: If the user's term clearly matches a glossary line above, use the right-hand English label "
            "from that line instead of transliterating the Gujarati token. Do not output the romanized/transliterated form "
            "when a matching glossary English label is available. Domain-specific disambiguation rules above override "
            "glossary lines if they conflict.\n"
        )

    system_content = (
        f"{domain_preamble}\n"
        "Translate the user's message to faithful spoken English for an internal agent. "
        "Respond with JSON: {\"translation\": \"...\"}.\n\n"
        "Do not preserve markdown, bullets, bracketed duplicates, or other formatting clutter, but do preserve the meaning uncertainty.\n"
        "When the input is garbled noise, random syllables, fragmentary, contradictory, or when any key noun, animal species, medicine, feed, product, disease, symptom, or requested action is uncertain, still provide the most faithful translation possible, using markers such as 'unclear animal', 'unclear feed name', 'unclear symptom', or '[unclear token]' instead of inventing missing meaning.\n"
        "Never convert a doubtful token into a specific medicine, feed, disease, animal species, or service term just because it would make a plausible livestock question."
    )

    return [
        {"role": "system", "content": system_content},
        {"role": "user", "content": text.strip()},
    ]


def _build_structured_pretranslation_prompt(
    source_name: str,
    source_code: str,
    text: str,
    conversation: Optional[Sequence[tuple[str, str]]] = None,
) -> str:
    """Build the structured translation prompt for non-OpenAI fallback models."""
    messages = _build_openai_pretranslation_messages(
        source_name, source_code, text, conversation
    )
    system_content = messages[0]["content"]
    user_content = messages[1]["content"]
    return (
        "<bos><start_of_turn>user\n"
        f"{system_content}\n\nUser message:\n{user_content}\n\n"
        'Respond only with valid JSON: {"translation": "..."}.'
        "<end_of_turn>\n"
        "<start_of_turn>model\n"
    )


async def _create_pretranslation_response(
    client: AsyncOpenAI,
    model: str,
    *,
    source_name: str,
    source_code: str,
    text: str,
    max_tokens: int,
    conversation: Optional[Sequence[tuple[str, str]]] = None,
):
    """Single OpenAI-compatible pretranslation call, parametrized by (client, model).

    Replaces the former ``_create_openai_pretranslation_response`` /
    ``_create_oss_pretranslation_response`` twins (identical bodies bar the model)."""
    return await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=_build_openai_pretranslation_messages(
                source_name, source_code, text, conversation
            ),
            max_completion_tokens=max_tokens,
            response_format={"type": "json_object"},
        ),
        timeout=settings.openai_pretranslation_timeout_seconds,
    )


def _extract_openai_message_diagnostics(response) -> dict:
    choice = response.choices[0] if getattr(response, "choices", None) else None
    message = getattr(choice, "message", None) if choice is not None else None
    usage = getattr(response, "usage", None)
    diagnostics = {
        "response_id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "finish_reason": getattr(choice, "finish_reason", None) if choice is not None else None,
        "content_present": bool(getattr(message, "content", None)) if message is not None else False,
        "refusal": getattr(message, "refusal", None) if message is not None else None,
        "tool_calls": len(getattr(message, "tool_calls", []) or []) if message is not None else 0,
        "usage": usage.model_dump() if hasattr(usage, "model_dump") else str(usage),
    }
    return diagnostics


def _raise_empty_pretranslation(
    response,
    *,
    source_lang: str,
    text: str,
) -> None:
    diagnostics = _extract_openai_message_diagnostics(response)
    logger.error(
        "OpenAI pretranslation returned empty output - source_lang=%s model=%s query_chars=%s query_preview=%r diagnostics=%s",
        source_lang,
        OPENAI_PRETRANSLATION_MODEL,
        len(text or ""),
        (text or "")[:160],
        diagnostics,
    )
    raise ValueError("GPT pretranslation returned empty output")


def _extract_translation_from_response(response) -> str:
    """Extract the translation string from an OpenAI JSON response."""
    raw = (response.choices[0].message.content or "").strip()
    return _extract_translation_from_raw(raw)


def _extract_translation_from_raw(raw: str) -> str:
    """Extract the translation string from raw model output."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        return (data.get("translation") or "").strip()
    except (json.JSONDecodeError, AttributeError):
        # Fallback: use raw content if JSON parsing fails
        return raw


async def translate_to_english_with_structured_fallback(
    text: str,
    source_lang: str,
    *,
    max_tokens: int = 1024,
    conversation: Optional[Sequence[tuple[str, str]]] = None,
) -> str:
    """Fallback pretranslation with the same structured contract as the OpenAI path.

    Returns the translated text, or an empty string on empty/failed output.
    """
    if not text or not text.strip():
        return text

    if source_lang.lower() in {"english", "en"}:
        return text

    source_name = LANG_NAMES.get(source_lang.lower(), source_lang.capitalize())
    source_code = LANG_CODES.get(source_lang.lower(), source_lang.lower())
    prompt = _build_structured_pretranslation_prompt(
        source_name, source_code, text, conversation
    )
    # Voice pretranslation structured fallback still speaks TranslateGemma
    # /completions directly (not the post-translation chain). Uses the SINGULAR
    # LB endpoint/model — the client-side endpoint list + _resolve_model were removed.
    endpoint = TRANSLATEGEMMA_27B_BASE_ENDPOINT
    model_id = TRANSLATEGEMMA_27B_BASE_MODEL

    async with aiohttp.ClientSession() as session:
        async with session.post(
            f"{endpoint}/completions",
            json={
                "model": model_id,
                "prompt": prompt,
                "temperature": 0.0,
                "max_tokens": max_tokens,
            },
            timeout=aiohttp.ClientTimeout(total=60),
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error("Structured fallback pretranslation API error %s: %s", response.status, error_text)
                raise Exception(f"Structured fallback pretranslation failed with status {response.status}")

            result = await response.json()
            raw_text = result["choices"][0]["text"].strip()
            translated_text = _extract_translation_from_raw(raw_text)
            translated_text = normalize_voice_output(translated_text, "english")
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            if not translated_text:
                logger.warning(
                    "Structured fallback pretranslation returned empty - source_lang=%s query=%r",
                    source_lang,
                    (text or "")[:100],
                )
                return text
            return translated_text


async def _translate_to_english_pretranslation(
    text: str,
    source_lang: str,
    *,
    client: AsyncOpenAI,
    model: str,
    label: str,
    translation_provider_label: str,
    extra_metadata: Optional[dict] = None,
    max_tokens: int = 1024,
    conversation: Optional[Sequence[tuple[str, str]]] = None,
) -> str:
    """Single parametrized pretranslation body — the collapse of the former
    ``translate_to_english_with_gpt5_mini`` / ``translate_to_english_with_oss_vllm``
    twins (identical bodies bar the client/model/langfuse-label). The two public
    wrappers below supply the managed-OpenAI vs OSS-vLLM (client, model, label);
    everything else — early-returns, glossary replacement, empty/timeout handling,
    the Langfuse ``query_pretranslation`` observation — is shared verbatim.

    Returns the translated text, or the original text on empty output.
    """
    if not text or not text.strip():
        return text

    if source_lang.lower() in {"english", "en"}:
        return text

    source_name = LANG_NAMES.get(source_lang.lower(), source_lang.capitalize())
    source_code = LANG_CODES.get(source_lang.lower(), source_lang.lower())

    langfuse = _get_langfuse()
    try:
        if not langfuse:
            response = await _create_pretranslation_response(
                client, model,
                source_name=source_name,
                source_code=source_code,
                text=text,
                max_tokens=max_tokens,
                conversation=conversation,
            )
            translated_text = _extract_translation_from_response(response)
            if not translated_text:
                logger.warning(
                    "%s pretranslation returned empty - source_lang=%s query=%r",
                    label, source_lang, (text or "")[:100],
                )
                return text
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            return translated_text

        with langfuse.start_as_current_observation(
            name="query_pretranslation",
            as_type="generation",
            input={
                "source_lang": source_lang,
                "target_lang": "english",
                "text": text,
            },
            model=model,
            metadata={
                "translation_provider": translation_provider_label,
                "pipeline_stage": "query_pretranslation",
                **(extra_metadata or {}),
            },
        ) as observation:
            response = await _create_pretranslation_response(
                client, model,
                source_name=source_name,
                source_code=source_code,
                text=text,
                max_tokens=max_tokens,
                conversation=conversation,
            )
            translated_text = _extract_translation_from_response(response)
            if not translated_text:
                logger.warning(
                    "%s pretranslation returned empty - source_lang=%s query=%r",
                    label, source_lang, (text or "")[:100],
                )
                observation.update(output="__EMPTY__")
                return text
            translated_text = _apply_exact_glossary_transliteration_replacements(text, translated_text)
            observation.update(output=translated_text)
            return translated_text
    except asyncio.TimeoutError as e:
        logger.error(
            "%s pretranslation timed out - source_lang=%s model=%s timeout_seconds=%.2f query_chars=%s query_preview=%r",
            label,
            source_lang,
            model,
            settings.openai_pretranslation_timeout_seconds,
            len(text or ""),
            (text or "")[:160],
        )
        raise TimeoutError(f"{label} pretranslation timed out") from e


async def translate_to_english_with_gpt5_mini(
    text: str,
    source_lang: str,
    *,
    max_tokens: int = 1024,
    session_id: str = "",
    user_id: str = "",
    process_id: str = "",
    pipeline_profile: str = "",
    conversation: Optional[Sequence[tuple[str, str]]] = None,
) -> str:
    """Pretranslate to English via the managed OpenAI client (legacy sessions).

    Thin wrapper over the unified ``_translate_to_english_pretranslation`` body;
    supplies the managed (client, model, label). Behaviour-identical to the former
    twin. Returns the translated text, or the original text on empty output.
    """
    return await _translate_to_english_pretranslation(
        text,
        source_lang,
        client=_get_openai_client(),
        model=OPENAI_PRETRANSLATION_MODEL,
        label="OpenAI",
        translation_provider_label=PRETRANSLATION_PROVIDER,
        max_tokens=max_tokens,
        conversation=conversation,
    )


async def translate_to_english_with_oss_vllm(
    text: str,
    source_lang: str,
    *,
    max_tokens: int = 1024,
    session_id: str = "",
    user_id: str = "",
    process_id: str = "",
    pipeline_profile: str = "",
    conversation: Optional[Sequence[tuple[str, str]]] = None,
) -> str:
    """Pretranslate via the OSS vLLM endpoint (per-request, sticky 'oss' sessions).

    Thin wrapper over the unified ``_translate_to_english_pretranslation`` body;
    supplies the OSS (client, model, label) + the ``pipeline_profile`` observation
    tag. Behaviour-identical to the former twin. Legacy sessions never hit this.
    """
    return await _translate_to_english_pretranslation(
        text,
        source_lang,
        client=_get_oss_pretranslation_client(),
        model=OSS_PRETRANSLATION_MODEL,
        label="OSS vLLM",
        translation_provider_label="vllm",
        extra_metadata={"pipeline_profile": pipeline_profile or "oss"},
        max_tokens=max_tokens,
        conversation=conversation,
    )


async def translate_text_stream_fast(
    text: str,
    source_lang: str,
    target_lang: str,
    model_size: Optional[Literal["4b", "12b", "27b", "27b-base"]] = None,
    temperature: float = 0.0,
    max_tokens: int = 2048
):
    """Stream translated text token by token (no artificial delay) via the
    post-translation tier chain [TranslateGemma(LB), managed-LLM overflow].

    First-token-commit semantics: if TranslateGemma fails BEFORE the first chunk on a
    fallbackable reason, the managed-LLM overflow tier transparently serves; a failure
    AFTER the first chunk propagates (the caller-side degrade net yields the English
    batch). Both tiers use the same incremental terminology normalizer before the
    remaining per-chunk Gujarati and voice-output cleanup."""
    if not text or not text.strip():
        return

    if _is_untranslatable_fragment(text):
        yield text
        return

    if source_lang.lower() == target_lang.lower():
        yield text
        return

    _prot = _protected_output_triggers(text, target_lang)

    instruction, tg_prompt = _prepare_translation_inputs(text, source_lang, target_lang)
    logger.info(f"Fast streaming translation {source_lang} -> {target_lang} via post-translation chain")

    chain = _post_translation_chain()

    def _make_stream(tier):
        if _is_translategemma_tier(tier):
            return _translategemma_stream(
                tier.handle, tg_prompt, source_lang, target_lang, text, temperature, max_tokens
            )
        return _llm_translation_stream(
            tier.handle, tier.model_name, instruction, source_lang, target_lang, text, temperature, max_tokens
        )

    try:
        base_stream = _stream_post_translation_chain(
            chain, _make_stream, source_lang=source_lang, target_lang=target_lang
        )
        stream = _buffered_protected_stream(base_stream, _prot) if _prot else base_stream
        async for chunk in stream:
            yield chunk
    except Exception as e:
        logger.error(f"Translation streaming error: {str(e)}")
        raise
