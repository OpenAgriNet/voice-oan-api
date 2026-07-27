"""Runtime holder + startup self-check for the unified pipeline (voice repo).

``configure()`` (called from the FastAPI lifespan) loads ``PIPELINE_CONFIG_PATH``
YAML when present, else synthesizes the config from the current env
(``legacy_shim``), validates it, stores it in the module global ``PIPELINE``, and
runs the resolvability self-check. ``get_pipeline()`` lazily configures on first
use so request paths and tests never see ``None``.

Self-check (P4): the unified config-driven pipeline is now the ONLY model-selection
path, so there is no longer a legacy wiring to compare against. The check resolves
every configured (profile, step) primary tier, logs the resolved
(provider, base_url, model, timeout), and WARNS — never raises — on any step that
fails to resolve, so a materialize edge case can never block startup. Genuine
config-shape errors are caught by ``PipelineConfig``'s validator at load time.
"""

from __future__ import annotations

import os
from typing import Optional

from helpers.utils import get_logger
from app.llm_core.config_model import PipelineConfig, Provider, Step
from app.llm_core.legacy_shim import synthesize_from_env

logger = get_logger(__name__)

PIPELINE: Optional[PipelineConfig] = None
# The config captured at ``configure()`` BEFORE any live (redis) refresh can
# override it — the deploy's permanent boot fallback. ``config_source`` reverts to
# THIS (not the last live config) when the live key is cleared/absent, so `clear`
# is a true emergency rollback to the boot config.
BOOT_PIPELINE: Optional[PipelineConfig] = None

# Providers the RAW_OPENAI factory can actually build (bare AsyncOpenAI clients).
# anthropic/gemini/translategemma are rejected for a RAW_OPENAI-kind step.
_RAW_OPENAI_PROVIDERS = frozenset({Provider.VLLM, Provider.OPENAI, Provider.AZURE})


class PipelineConfigError(ValueError):
    """Raised at startup for a structurally-valid but unbuildable pipeline config
    (e.g. an anthropic/gemini tier on a RAW_OPENAI-kind step). Fails the boot fast
    instead of letting it crash per-request. ``main`` re-raises it."""


def validate_config(pipeline: PipelineConfig) -> None:
    """Startup validation (plan §2, MUST-FIX E): reject any RAW_OPENAI-kind step
    (``pre_translation`` / ``moderation`` / ``non_meaningful``) whose tier provider
    the factory cannot build — i.e. not in {vllm, openai, azure-openai}. These
    steps materialize as bare ``AsyncOpenAI`` clients, so anthropic/gemini would
    only crash mid-request; catching it at boot turns a per-request 500 into a
    clear startup failure.

    Supporting anthropic/gemini for RAW pretranslation is a tracked enhancement
    (see ``UNIFIED_LLM_PIPELINE_PLAN.md`` §2 — file an issue to add a native
    pretranslation client for those providers)."""
    from app.llm_core.config_model import StepClientKind
    from app.llm_core.resolver import STEP_CLIENT_KIND

    raw_steps = {s for s, kind in STEP_CLIENT_KIND.items() if kind is StepClientKind.RAW_OPENAI}
    bad: list[str] = []

    def _check(where: str, step: Step, step_cfg) -> None:
        if step not in raw_steps:
            return
        for tier in step_cfg.tiers:
            if tier.provider not in _RAW_OPENAI_PROVIDERS:
                bad.append(f"{where} step={step.value} provider={tier.provider.value}")

    for profile in pipeline.profiles:
        for step, step_cfg in profile.steps.items():
            _check(f"profile={profile.name}", step, step_cfg)
    for step, step_cfg in pipeline.defaults.items():
        _check("defaults", step, step_cfg)

    if bad:
        raise PipelineConfigError(
            "llm_core config invalid: RAW_OPENAI steps (pre_translation/moderation/"
            "non_meaningful) accept only vllm/openai/azure-openai providers, but "
            "found: " + "; ".join(sorted(bad)) + ". Set PRETRANSLATION_PROVIDER / "
            "VOICE_MODERATION_PROVIDER / VOICE_NON_MEANINGFUL_PROVIDER (or the YAML "
            "tier) to a supported provider. (anthropic/gemini RAW pretranslation is "
            "a tracked enhancement, not yet supported.)"
        )


def validate_content(cfg: PipelineConfig) -> None:
    """Run the SAME content gates the boot path applies against a CANDIDATE config
    (a live redis config, or an ops-script payload) BEFORE it goes live — raising on
    any unbuildable content. This is the single validator both ``config_source``
    (fail-CLOSED live load) and ``scripts/set_pipeline_config.py`` (refuse-to-write)
    call, so a schema-valid but unbuildable config can never go live and break
    requests.

    Two checks, mirroring boot:
      (a) ``validate_config(cfg)`` — provider/step legality (an anthropic/gemini tier
          on a RAW_OPENAI step, etc.); raises ``PipelineConfigError``; and
      (b) a resolvability probe — for every profile, for every CONFIGURED step, build
          the primary tier handle via ``resolver.primary_tier``; the factory raises on
          an unbuildable tier (vllm tier with no endpoint, azure tier missing
          api_key_env/api_version, etc.), exactly as the boot self-check would.

    The resolver reads ``runtime.get_pipeline()``, so the probe is run with ``cfg``
    temporarily installed as ``PIPELINE`` and the live source suppressed (so the
    nested ``get_pipeline`` neither re-reads redis nor recurses); both are restored
    in a ``finally``. Raises (never swallows) so callers can fail closed."""
    global PIPELINE
    validate_config(cfg)

    from app.llm_core import resolver, config_source

    prev_pipeline = PIPELINE
    prev_suppress = config_source._suppress_refresh
    PIPELINE = cfg
    config_source._suppress_refresh = True
    try:
        for profile in cfg.profiles:
            for step in Step:
                if cfg.step_config(profile, step) is None:
                    continue  # a profile need not configure every step (probe only what's set)
                # Builds the primary handle; raises on an unbuildable tier.
                resolver.primary_tier(step, profile.name)
    finally:
        config_source._suppress_refresh = prev_suppress
        PIPELINE = prev_pipeline


def _load_from_yaml(path: str) -> PipelineConfig:
    import yaml  # lazy: only needed when a config file is supplied

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return PipelineConfig(**data)


def _truthy_env(name: str) -> bool:
    v = os.getenv(name)
    return v is not None and v.strip().lower() in {"1", "true", "yes", "on"}


class BootRefused(RuntimeError):
    """Intentional hard-gate boot failure (e.g. REQUIRE_OVERFLOW_ARMED with overflow
    DISARMED). Distinct type so the best-effort ``configure()`` call site in main.py
    can re-raise it (a deliberate refusal to boot) while still swallowing genuine
    non-fatal configure/self-check edge cases."""


def _assert_boot_posture() -> None:
    """Emit a LOUD one-line 'overflow ARMED / DISARMED' posture summary at boot.

    The whole overflow system — the OSS->managed attempt chain AND the health +
    concurrency guards, which fire ONLY via the fallback walkers — is inert unless
    ``FALLBACK_ENABLED`` is on. A deploy from defaults could therefore ship dark
    with nothing in the logs saying so. This line makes the armament state greppable
    at startup (``grep 'llm_core posture'``): INFO when armed, WARNING when disarmed.

    Honors the opt-in ``REQUIRE_OVERFLOW_ARMED``: when truthy, a DISARMED boot is a
    hard error (raises) so prod can gate on it and never ship overflow-off."""
    from app.config import settings

    def _onoff(b: bool) -> str:
        return "on" if b else "off"

    fallback_on = bool(settings.fallback_enabled)
    if settings.concurrency_gauge_enabled:
        conc = "on(metrics_url set)" if settings.agent_concurrency_metrics_url else "on(metrics_url unset — no-op)"
    else:
        conc = "off"
    guards = (
        f"health_breaker={_onoff(settings.health_breaker_enabled)} "
        f"health_poller={_onoff(settings.health_poller_enabled)} "
        f"concurrency={conc}"
    )
    if fallback_on:
        logger.info("llm_core posture: overflow=ARMED fallback=on %s", guards)
    else:
        logger.warning(
            "llm_core posture: overflow=DISARMED (FALLBACK_ENABLED=false) — "
            "health/concurrency guards inert (they fire only via the fallback "
            "walkers); %s", guards,
        )
        if _truthy_env("REQUIRE_OVERFLOW_ARMED"):
            raise BootRefused(
                "llm_core boot refused: REQUIRE_OVERFLOW_ARMED=true but overflow is "
                "DISARMED (FALLBACK_ENABLED=false). Set FALLBACK_ENABLED=true to arm "
                "the unified overflow/fallback path, or unset REQUIRE_OVERFLOW_ARMED."
            )


def configure(*, run_self_check: bool = True) -> PipelineConfig:
    """Load / synthesize the pipeline config, validate, store, self-check."""
    global PIPELINE, BOOT_PIPELINE
    path = os.getenv("PIPELINE_CONFIG_PATH")
    if path and os.path.exists(path):
        logger.info("llm_core: loading pipeline config from %s", path)
        PIPELINE = _load_from_yaml(path)
    else:
        PIPELINE = synthesize_from_env()
        logger.info(
            "llm_core: synthesized pipeline config from env (profiles=%s)",
            [f"{p.name}:{p.weight}" for p in PIPELINE.profiles],
        )
    # Fail-fast config validation (E). The unified pipeline is the only path after
    # P4 (the LLM_CORE_ENABLED kill-switch was removed), so this config always
    # drives requests and must always be buildable: validate unconditionally.
    validate_config(PIPELINE)
    # Capture the boot config as the permanent fallback BEFORE any live redis
    # refresh can override PIPELINE (get_pipeline -> config_source.maybe_refresh).
    # config_source reverts to THIS on a cleared/absent live key (emergency
    # rollback), never to a stale last-live config.
    BOOT_PIPELINE = PIPELINE
    # Tracing-only: dump the COMPLETE loaded config (all profiles, step tiers,
    # triggers) as one structured boot log line so the full wiring is greppable
    # in logs even before any turn arrives (`grep llm_core.full_config`).
    from app.llm_core import trace as _trace
    _trace.log_full_config(PIPELINE)
    # Boot posture assertion: LOUD ARMED/DISARMED overflow summary (+ hard-gate via
    # REQUIRE_OVERFLOW_ARMED). Placed after config load so a hard-gate raise fires
    # before the (non-fatal) self-check.
    _assert_boot_posture()
    # M2: note whether the live redis-backed config source is enabled (default OFF).
    # When on, weight changes PUT to the channel key take effect within the TTL with
    # no redeploy; when off, get_pipeline() serves the boot config only.
    from app.llm_core import config_source
    if config_source.enabled():
        logger.info(
            "llm_core: live redis config source ENABLED (channel=%s key=%s refresh=%ss) "
            "— weight changes PUT to that key take effect within the TTL, no redeploy",
            config_source.channel(), config_source.key(), config_source.refresh_interval_s(),
        )
    else:
        logger.info(
            "llm_core: live redis config source disabled (%s unset) — serving boot config only",
            config_source.ENABLED_ENV,
        )
    if run_self_check:
        try:
            self_check()
        except Exception as exc:  # never break config load on a self-check bug
            logger.warning("llm_core: self-check skipped (%s)", exc)
    return PIPELINE


def get_pipeline() -> PipelineConfig:
    global PIPELINE
    if PIPELINE is None:
        configure(run_self_check=False)
    assert PIPELINE is not None
    # M2 (live config): consult the redis-backed source. TTL-gated (hits redis at
    # most once per PIPELINE_CONFIG_REFRESH_S window) and fail-safe (any error ->
    # returns the last-good PIPELINE unchanged, never raises). When
    # PIPELINE_CONFIG_REDIS_ENABLED is unset/false this is an immediate identity
    # no-op, so behaviour is byte-identical to boot-config-only.
    from app.llm_core import config_source
    PIPELINE = config_source.maybe_refresh(PIPELINE)
    return PIPELINE


def _base_url(handle) -> Optional[str]:
    b = getattr(handle, "base_url", None)
    if b is None:
        b = getattr(getattr(handle, "client", None), "base_url", None)
    return str(b).rstrip("/") if b is not None else None


def self_check() -> None:
    """Startup validation: every profile's every step must resolve to a live
    primary tier (build a handle without raising) for the current config.

    This is the P4 successor to the P0/P1 identity self-check. There is no longer a
    legacy wiring to compare against — the unified pipeline is the only path — so
    the check now just logs the resolved (provider, base_url, model, timeout) per
    configured step and WARNS on any step that fails to resolve. It is
    intentionally non-fatal: a materialize edge case (e.g. a fallback-tier key
    absent in this env) must never block startup, exactly as the flag-off boot was
    robust before. Genuine config-shape errors are already caught by
    ``PipelineConfig``'s validator at load time.
    """
    from app.llm_core import resolver

    pipeline = get_pipeline()
    failures: list[str] = []

    for profile in pipeline.profiles:
        for step in Step:
            step_cfg = pipeline.step_config(profile, step)
            if step_cfg is None:
                continue  # a profile need not configure every step (post-trans lives in defaults)
            try:
                # Resolve BY PROFILE NAME (N-way): a broken 3rd-profile tier (bad
                # provider/endpoint/key) is caught here at boot, not just oss/managed.
                mt = resolver.primary_tier(step, profile.name)
                logger.info(
                    "llm_core self-check profile=%s step=%s -> provider=%s base_url=%s model=%s timeout=%s",
                    profile.name, step.value, mt.provider, _base_url(mt.handle), mt.model_name, mt.timeout,
                )
            except Exception as exc:
                failures.append(f"{profile.name}/{step.value}: {type(exc).__name__}: {exc}")

    if failures:
        logger.warning(
            "llm_core self-check: %d step(s) did not resolve in this env (non-fatal):\n  - %s",
            len(failures), "\n  - ".join(failures),
        )
    else:
        logger.info(
            "llm_core self-check PASSED: every configured step resolves (profiles=%s)",
            [p.name for p in pipeline.profiles],
        )
