"""Identity shim: synthesize a :class:`PipelineConfig` from TODAY's env vars (voice).

``synthesize_from_env()`` reads the env exactly as the current voice wiring reads
it — ``agents/models`` (managed + OSS agent models), ``translation.py``
(pre/post-translation + the RAW_OPENAI clients shared by moderation and the
non-meaningful classifier), ``pipeline_router`` (the OSS %-split) and the
``FALLBACK_*`` timeouts — and emits an equivalent config so that, with
``LLM_CORE_ENABLED`` on, the resolver reproduces the legacy provider / base_url /
model / timeout for the current environment. No legacy env reading is removed;
this is a parallel, additive reader.

Voice specifics vs the chat shim:
  * there is a ``non_meaningful`` step and no ``suggestions`` step;
  * ``moderation`` / ``non_meaningful`` / ``pre_translation`` are all RAW_OPENAI
    steps that share the SAME two underlying clients as pretranslation
    (``translation._get_openai_client`` for managed, ``_get_oss_pretranslation_client``
    for OSS) and the SAME two models (``OPENAI_PRETRANSLATION_MODEL`` /
    ``OSS_PRETRANSLATION_MODEL``) — NOT the agent model. So their tiers are built
    from the pretranslation env, not from ``LLM_MODEL_NAME``;
  * ``moderation``'s provider is governed by ``VOICE_MODERATION_PROVIDER`` and
    ``non_meaningful``'s by ``VOICE_NON_MEANINGFUL_PROVIDER`` — both independent of
    the session variant (mirroring the legacy helpers exactly).

Profiles: ``[oss(weight=OSS_PIPELINE_PCT), managed(100-pct)]`` when OSS is
configured (``OSS_INFERENCE_ENDPOINT_URL`` set), else ``[managed(100)]``. For the
OSS profile each variant-sensitive step (agent, pre_translation, moderation)
carries ``[oss, managed]`` tiers (mirroring ``fallback.attempt_chain`` +
``moderation._client_model_for_kind``); managed carries ``[managed]``.
``non_meaningful`` has no fallback wiring today, so it is a single-tier,
profile-invariant step in ``defaults``. Post-translation (TranslateGemma) is
likewise profile-invariant and lives in ``defaults``.

Kept free of ``agents.*`` / ``app.services.*`` imports — reads os.getenv only —
so the core stays import-clean (and importable under the local pydantic-ai
mismatch that breaks ``agents.models`` / ``app.services.translation``).
"""

from __future__ import annotations

import logging
import os

from app.llm_core.config_model import (
    ApiStyle,
    ConcurrencyGate,
    NamedProfile,
    PipelineConfig,
    Provider,
    Step,
    StepConfig,
    Tier,
    Triggers,
)


logger = logging.getLogger(__name__)


def _env(name: str, default: str | None = None) -> str | None:
    return os.getenv(name, default)


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ── managed agent tier (agents/models LLM_MODEL) ──────────────────────────────
def _managed_agent_tier(timeout_ms: int, label: str) -> Tier:
    provider = (_env("LLM_PROVIDER", "openai") or "openai").lower()
    model = _env("LLM_MODEL_NAME") or "gpt-4.1"

    if provider == "vllm":
        return Tier(provider=Provider.VLLM, model=model, endpoint=_env("INFERENCE_ENDPOINT_URL"),
                    api_key_env="INFERENCE_API_KEY", timeout_ms=timeout_ms, label=label)
    if provider == "anthropic":
        return Tier(provider=Provider.ANTHROPIC, model=model,
                    api_key_env="ANTHROPIC_API_KEY", timeout_ms=timeout_ms, label=label)
    if provider == "gemini":
        return Tier(provider=Provider.GEMINI, model=model,
                    api_key_env="GEMINI_API_KEY", timeout_ms=timeout_ms, label=label)
    if provider == "azure-openai":
        return Tier(provider=Provider.AZURE, model=_env("AZURE_OPENAI_DEPLOYMENT_NAME", model) or model,
                    endpoint=_env("AZURE_OPENAI_ENDPOINT"), api_key_env="AZURE_OPENAI_API_KEY",
                    api_version=_env("AZURE_OPENAI_API_VERSION"), timeout_ms=timeout_ms, label=label)
    # default: openai
    return Tier(provider=Provider.OPENAI, model=model, endpoint=None,
                api_key_env="OPENAI_API_KEY", timeout_ms=timeout_ms, label=label)


def _oss_agent_tier(timeout_ms: int, label: str) -> Tier:
    return Tier(
        provider=Provider.VLLM,
        model=_env("OSS_LLM_MODEL_NAME", "gemma-4-31b-it") or "gemma-4-31b-it",
        endpoint=_env("OSS_INFERENCE_ENDPOINT_URL"),
        api_key_env="OSS_INFERENCE_API_KEY",
        timeout_ms=timeout_ms,
        label=label,
    )


# ── RAW_OPENAI models shared by pre_translation / moderation / non_meaningful ──
# Mirror translation.py's module-level resolution EXACTLY.
def _pretranslation_provider() -> str:
    llm_provider = (_env("LLM_PROVIDER", "openai") or "openai").lower()
    return (_env("PRETRANSLATION_PROVIDER", llm_provider) or llm_provider).lower()


def _openai_pretranslation_model() -> str:
    """translation.OPENAI_PRETRANSLATION_MODEL: PRETRANSLATION_MODEL, else
    OPENAI_PRETRANSLATION_MODEL, else the provider default (vllm ->
    LLM_MODEL_NAME/gemma-4-31b-it; otherwise gpt-5.1)."""
    if _pretranslation_provider() == "vllm":
        default = _env("LLM_MODEL_NAME", "gemma-4-31b-it") or "gemma-4-31b-it"
    else:
        default = "gpt-5.1"
    return _env("PRETRANSLATION_MODEL", _env("OPENAI_PRETRANSLATION_MODEL", default)) or default


def _oss_pretranslation_model() -> str:
    """translation.OSS_PRETRANSLATION_MODEL."""
    return _env("OSS_PRETRANSLATION_MODEL", _env("OSS_LLM_MODEL_NAME", "gemma-4-31b-it")) or "gemma-4-31b-it"


def _managed_raw_tier(timeout_ms: int, label: str) -> Tier:
    """The "managed" RAW_OPENAI tier = translation._get_openai_client() +
    OPENAI_PRETRANSLATION_MODEL. When PRETRANSLATION_PROVIDER=vllm this points at
    the local INFERENCE_ENDPOINT_URL (a vLLM box), else at OpenAI."""
    model = _openai_pretranslation_model()
    if _pretranslation_provider() == "vllm":
        return Tier(provider=Provider.VLLM, model=model, endpoint=_env("INFERENCE_ENDPOINT_URL"),
                    api_key_env="INFERENCE_API_KEY", timeout_ms=timeout_ms, label=label)
    return Tier(provider=Provider.OPENAI, model=model, endpoint=None,
                api_key_env="OPENAI_API_KEY", timeout_ms=timeout_ms, label=label)


def _oss_raw_tier(timeout_ms: int, label: str) -> Tier:
    """The "oss" RAW_OPENAI tier = translation._get_oss_pretranslation_client() +
    OSS_PRETRANSLATION_MODEL, pinned to the OSS vLLM endpoint."""
    return Tier(
        provider=Provider.VLLM,
        model=_oss_pretranslation_model(),
        endpoint=_env("OSS_INFERENCE_ENDPOINT_URL"),
        api_key_env="OSS_INFERENCE_API_KEY",
        timeout_ms=timeout_ms,
        label=label,
    )


def _non_meaningful_tier() -> Tier:
    """Single-tier, profile-invariant. Provider governed by
    VOICE_NON_MEANINGFUL_PROVIDER (default vllm), independent of session variant —
    exactly non_meaningful._non_meaningful_client_and_model()."""
    provider = (_env("VOICE_NON_MEANINGFUL_PROVIDER", "vllm") or "vllm").strip().lower()
    if provider == "openai":
        return _managed_raw_tier(0, "non-meaningful")
    return _oss_raw_tier(0, "non-meaningful")


# ── post-translation — profile-invariant → defaults ──────────────────────────
# Chain: [TranslateGemma(LB), managed-LLM overflow]. TranslateGemma is deployed
# BEHIND AN NGINX LB, so the SINGULAR ``TRANSLATEGEMMA_27B_BASE_ENDPOINT`` IS that
# LB (it fans out to replicas server-side) — there is exactly one client-facing
# endpoint, never a client-side list. The overflow tier is the managed LLM doing
# en→target translation via chat.completions with the SAME glossary/rules prompt;
# it serves only when TranslateGemma fails before the first streamed token.
def _post_translation_tiers() -> list[Tier]:
    # TranslateGemma is fronted by an nginx LB, so post-translation reads the
    # SINGULAR endpoint. The old client-side plural list (+random.choice) is gone;
    # warn loudly if a stale env still sets only the plural var, which would
    # otherwise be silently ignored and drop TG to the localhost default.
    if _env("TRANSLATEGEMMA_27B_BASE_ENDPOINTS") and not _env("TRANSLATEGEMMA_27B_BASE_ENDPOINT"):
        logger.warning(
            "TRANSLATEGEMMA_27B_BASE_ENDPOINTS (plural) is set but the singular "
            "TRANSLATEGEMMA_27B_BASE_ENDPOINT is not — the plural var is DEPRECATED "
            "and ignored; TranslateGemma will fall back to the localhost default. "
            "Set TRANSLATEGEMMA_27B_BASE_ENDPOINT to the nginx LB URL."
        )
    endpoint = _env("TRANSLATEGEMMA_27B_BASE_ENDPOINT", "http://localhost:18002/v1") or "http://localhost:18002/v1"
    model_id = _env("TRANSLATEGEMMA_27B_BASE_MODEL", "translategemma-27b-base") or "translategemma-27b-base"
    tg = Tier(
        provider=Provider.TRANSLATEGEMMA, model=model_id, endpoint=endpoint,
        api_style=ApiStyle.TEXT_COMPLETION, timeout_ms=60000, label="translategemma",
    )
    # Cross-provider overflow = the managed agent tier, but forced to CHAT api_style
    # and given its own (shorter) first-token deadline. Reuses the managed builder so
    # provider/model/key/endpoint track LLM_PROVIDER exactly.
    llm_ms = _int_env("FALLBACK_POST_TRANSLATION_LLM_TIMEOUT_MS", 30000)
    llm_fallback = _managed_agent_tier(llm_ms, "llm-fallback").model_copy(
        update={"api_style": ApiStyle.CHAT}
    )
    return [tg, llm_fallback]


def _oss_configured() -> bool:
    return bool(_env("OSS_INFERENCE_ENDPOINT_URL"))


def _agent_triggers(ttft_ms: int) -> Triggers:
    """AGENT-step triggers: the TTFT deadline plus, when
    ``AGENT_CONCURRENCY_METRICS_URL`` is set, an explicit ``ConcurrencyGate`` so
    the P3 gauge can deprioritize the vLLM tier under load. The metrics URL is
    given EXPLICITLY (never derived from the inference endpoint); unset -> no gate
    -> the reorder is a no-op even with CONCURRENCY_GAUGE_ENABLED on."""
    metrics_url = _env("AGENT_CONCURRENCY_METRICS_URL")
    gate = (
        ConcurrencyGate(metrics_url=metrics_url, max_concurrency=_int_env("CONCURRENCY_MAX", 10))
        if metrics_url
        else None
    )
    return Triggers(ttft_deadline_ms=ttft_ms, concurrency_gate=gate)


def synthesize_from_env() -> PipelineConfig:
    """Build a behaviour-identical PipelineConfig from the current environment."""
    managed_ms = _int_env("FALLBACK_MANAGED_TIMEOUT_MS", 20000)
    oss_chat_ms = _int_env("FALLBACK_CHAT_OSS_TIMEOUT_MS", 8000)
    oss_mod_ms = _int_env("FALLBACK_MODERATION_OSS_TIMEOUT_MS", 5000)
    oss_pre_ms = _int_env("FALLBACK_PRETRANSLATION_OSS_TIMEOUT_MS", 10000)

    fallback_enabled = (os.getenv("FALLBACK_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
    sticky_ttl = _int_env("OSS_VARIANT_TTL", 60 * 60 * 24 * 7)

    managed_agent = _managed_agent_tier(managed_ms, "managed-agent")
    managed_raw = _managed_raw_tier(managed_ms, "managed-pretranslation")

    # Profile-invariant steps live in defaults: non_meaningful (no fallback wiring
    # today) + post_translation (TranslateGemma, identical for both variants).
    defaults = {
        Step.NON_MEANINGFUL: StepConfig(tiers=[_non_meaningful_tier()]),
        Step.POST_TRANSLATION: StepConfig(tiers=_post_translation_tiers()),
    }

    def managed_steps() -> dict:
        agent_cfg = StepConfig(tiers=[managed_agent], triggers=_agent_triggers(managed_ms))
        return {
            Step.AGENT: agent_cfg,
            Step.MODERATION: StepConfig(tiers=[managed_raw]),
            Step.PRE_TRANSLATION: StepConfig(tiers=[managed_raw]),
        }

    if not _oss_configured():
        managed = NamedProfile(name="managed", weight=100, steps=managed_steps())
        return PipelineConfig(
            profiles=[managed],
            defaults=defaults,
            sticky_ttl_s=sticky_ttl,
            fallback_enabled=fallback_enabled,
        )

    # OSS configured: two profiles. OSS profile carries [oss, managed] per
    # variant-sensitive step (mirrors fallback.attempt_chain +
    # moderation._client_model_for_kind); managed carries [managed].
    pct = max(0, min(100, _int_env("OSS_PIPELINE_PCT", 0)))
    oss_steps = {
        Step.AGENT: StepConfig(
            tiers=[_oss_agent_tier(oss_chat_ms, "oss-agent"), managed_agent],
            triggers=_agent_triggers(oss_chat_ms),
        ),
        Step.MODERATION: StepConfig(tiers=[_oss_raw_tier(oss_mod_ms, "oss-moderation"), managed_raw]),
        Step.PRE_TRANSLATION: StepConfig(tiers=[_oss_raw_tier(oss_pre_ms, "oss-pretranslation"), managed_raw]),
    }
    oss_profile = NamedProfile(name="oss", weight=pct, steps=oss_steps)
    managed_profile = NamedProfile(name="managed", weight=100 - pct, steps=managed_steps())
    return PipelineConfig(
        profiles=[oss_profile, managed_profile],
        defaults=defaults,
        sticky_ttl_s=sticky_ttl,
        fallback_enabled=fallback_enabled,
    )
