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
    global PIPELINE
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
    # Tracing-only: dump the COMPLETE loaded config (all profiles, step tiers,
    # triggers) as one structured boot log line so the full wiring is greppable
    # in logs even before any turn arrives (`grep llm_core.full_config`).
    from app.llm_core import trace as _trace
    _trace.log_full_config(PIPELINE)
    # Boot posture assertion: LOUD ARMED/DISARMED overflow summary (+ hard-gate via
    # REQUIRE_OVERFLOW_ARMED). Placed after config load so a hard-gate raise fires
    # before the (non-fatal) self-check.
    _assert_boot_posture()
    if run_self_check:
        try:
            self_check()
        except Exception as exc:  # never break config load on a self-check bug
            logger.warning("llm_core: self-check skipped (%s)", exc)
    return PIPELINE


def get_pipeline() -> PipelineConfig:
    if PIPELINE is None:
        configure(run_self_check=False)
    assert PIPELINE is not None
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
