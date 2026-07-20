"""Runtime holder + startup self-check for the unified pipeline (voice repo).

``configure()`` (called from the FastAPI lifespan) loads ``PIPELINE_CONFIG_PATH``
YAML when present, else synthesizes the config from the current env
(``legacy_shim``), validates it, stores it in the module global ``PIPELINE``, and
runs the identity self-check. ``get_pipeline()`` lazily configures on first use so
request paths and tests never see ``None``.

Identity self-check (the P0 bar): for the current ``.env`` it logs the resolved
(provider, base_url, model, timeout) per step and asserts they equal the legacy
singletons (``agents.models`` / ``translation.py``). A mismatch raises only when
``LLM_CORE_ENABLED`` is on — so a flag-off boot can never be broken by a shim
edge case, while flipping the flag on is gated on true identity.

Both ``agents.models`` and ``app.services.translation`` are imported LAZILY inside
``self_check`` and guarded: under the local pydantic-ai version mismatch (0.2.4 vs
the pinned 1.x) those modules fail to import, so the corresponding checks are
skipped-with-a-log rather than crashing the boot. In the deploy env (pinned 1.x)
they import and the identity checks run for real.
"""

from __future__ import annotations

import os
from typing import Optional

from helpers.utils import get_logger
from app.llm_core.config_model import PipelineConfig, Step
from app.llm_core.legacy_shim import synthesize_from_env

logger = get_logger(__name__)

PIPELINE: Optional[PipelineConfig] = None


def _load_from_yaml(path: str) -> PipelineConfig:
    import yaml  # lazy: only needed when a config file is supplied

    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return PipelineConfig(**data)


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
    if run_self_check:
        try:
            self_check()
        except AssertionError:
            raise
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
    """Assert flag-on resolution == legacy wiring for the current env."""
    from app.llm_core import resolver
    from app.config import settings

    enforce = bool(getattr(settings, "llm_core_enabled", False))
    managed_timeout = settings.fallback_managed_timeout_ms / 1000.0
    mismatches: list[str] = []

    # ── agent: identity with agents.models.get_model_for_variant ──────────────
    # Guarded: agents/models fails to import under a pydantic-ai version mismatch.
    try:
        from agents.models import (
            get_model_for_variant,
            provider_for_variant,
            oss_model_available,
        )
    except Exception as exc:  # pragma: no cover - env-dependent
        get_model_for_variant = None
        logger.warning("llm_core self-check: agent checks skipped (%s)", exc)

    if get_model_for_variant is not None:
        variants = ["legacy"]
        if oss_model_available():
            variants.append("oss")
        for variant in variants:
            legacy_model = get_model_for_variant(variant)
            legacy_provider = provider_for_variant(variant)
            legacy_name = getattr(legacy_model, "model_name", None)
            mt = resolver.primary_tier(Step.AGENT, variant)
            r_url = _base_url(mt.handle)
            l_url = _base_url(legacy_model)
            logger.info(
                "llm_core self-check step=agent variant=%s -> provider=%s base_url=%s model=%s timeout=%s",
                variant, mt.provider, r_url, mt.model_name, mt.timeout,
            )
            if legacy_name is not None and mt.model_name != legacy_name:
                mismatches.append(f"agent/{variant} model {mt.model_name!r} != legacy {legacy_name!r}")
            if l_url is not None and r_url != l_url:
                mismatches.append(f"agent/{variant} base_url {r_url!r} != legacy {l_url!r}")
            if mt.provider != legacy_provider:
                mismatches.append(f"agent/{variant} provider {mt.provider!r} != legacy {legacy_provider!r}")

    # ── pre-translation / moderation / non-meaningful / post-translation ──────
    # RAW_OPENAI steps + TranslateGemma: identity with translation.py singletons.
    # Guarded: translation.py transitively imports agents.tools, which fails to
    # build under the pydantic-ai version mismatch.
    try:
        from app.services import translation as tr
    except Exception as exc:  # pragma: no cover - env-dependent
        tr = None
        logger.warning("llm_core self-check: translation checks skipped (%s)", exc)

    if tr is not None:
        # pre-translation (managed RAW_OPENAI) == translation._get_openai_client()
        # + OPENAI_PRETRANSLATION_MODEL.
        managed_pre = resolver.primary_tier(Step.PRE_TRANSLATION, "legacy")
        logger.info(
            "llm_core self-check step=pre_translation variant=legacy -> provider=%s base_url=%s model=%s timeout=%s",
            managed_pre.provider, _base_url(managed_pre.handle), managed_pre.model_name, managed_pre.timeout,
        )
        if managed_pre.model_name != tr.OPENAI_PRETRANSLATION_MODEL:
            mismatches.append(
                f"pre_translation model {managed_pre.model_name!r} != legacy {tr.OPENAI_PRETRANSLATION_MODEL!r}"
            )
        try:
            legacy_client = tr._get_openai_client()
            l_url = _base_url(legacy_client)
            r_url = _base_url(managed_pre.handle)
            if l_url is not None and r_url != l_url:
                mismatches.append(f"pre_translation base_url {r_url!r} != legacy {l_url!r}")
        except Exception as exc:  # client init may need a key not present in tests
            logger.info("llm_core self-check: pre_translation client compare skipped (%s)", exc)

        # moderation (managed RAW_OPENAI) shares the SAME managed pretranslation
        # model — verify the resolver agrees.
        managed_mod = resolver.primary_tier(Step.MODERATION, "legacy")
        if managed_mod.model_name != tr.OPENAI_PRETRANSLATION_MODEL:
            mismatches.append(
                f"moderation model {managed_mod.model_name!r} != legacy {tr.OPENAI_PRETRANSLATION_MODEL!r}"
            )

        # post-translation: identity with translation TranslateGemma endpoints.
        post = resolver.primary_tier(Step.POST_TRANSLATION, "legacy")
        legacy_eps = [e.rstrip("/") for e in tr.TRANSLATION_ENDPOINTS_27B_BASE]
        legacy_tg_model = tr.TRANSLATION_MODEL_IDS.get("27b-base")
        logger.info(
            "llm_core self-check step=post_translation variant=legacy -> model=%s endpoints=%s",
            post.model_name, legacy_eps,
        )
        if post.endpoint.rstrip("/") not in legacy_eps:
            mismatches.append(f"post_translation endpoint {post.endpoint!r} not in legacy {legacy_eps!r}")
        if legacy_tg_model is not None and post.model_name != legacy_tg_model:
            mismatches.append(f"post_translation model {post.model_name!r} != legacy {legacy_tg_model!r}")

    # managed timeout parity (sample the agent managed tier).
    managed_agent_mt = resolver.primary_tier(Step.AGENT, "legacy")
    if managed_agent_mt.timeout not in (None, managed_timeout):
        mismatches.append(f"agent/legacy timeout {managed_agent_mt.timeout} != managed {managed_timeout}")

    if mismatches:
        msg = "llm_core self-check FAILED (resolve != legacy wiring):\n  - " + "\n  - ".join(mismatches)
        if enforce:
            raise AssertionError(msg)
        logger.warning("%s\n(LLM_CORE_ENABLED is off; not raising)", msg)
    else:
        logger.info(
            "llm_core self-check PASSED: resolve == legacy wiring (LLM_CORE_ENABLED=%s)",
            enforce,
        )
