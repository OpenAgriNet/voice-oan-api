"""Unit tests for the unified LLM pipeline core (app/llm_core), P0 — voice repo.

Covers: factory superset (each provider builds the right handle kind + carries
base_url/key), shim identity (synthesize_from_env reproduces the legacy voice env
wiring, incl. the non_meaningful step + RAW_OPENAI moderation), resolver returns a
non-empty chain, and the default-OFF flag posture.

Zero network: building a pydantic-ai Model / AsyncOpenAI client is lazy (no call
is made), and no test invokes a model. Sets dummy keys before importing app code.

NOTE (env): the locally-installed pydantic-ai (0.2.4) vs the repo-pinned 1.x
breaks importing ``agents.models`` (voice's ``agents/models/__init__.py`` uses
``OpenAIChatModel`` unconditionally) AND ``app.services.translation`` (imports
``agents.tools``). The factory is version-tolerant so ``app.llm_core`` imports
cleanly regardless; tests that need the legacy singletons for a real identity
compare are guarded with ``importorskip``.
"""

import os

os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest

from app.llm_core import (
    Provider,
    Step,
    StepClientKind,
    Tier,
    ApiStyle,
    build_handle,
    materialize,
    synthesize_from_env,
    resolver,
    runtime,
)
from app.llm_core.factory import TGDescriptor, MaterializedTier


def _openai_model_types() -> tuple[str, ...]:
    # pydantic-ai 1.x -> OpenAIChatModel; older -> OpenAIModel.
    return ("OpenAIChatModel", "OpenAIModel")


# ── factory superset ──────────────────────────────────────────────────────────

def test_factory_vllm_agent_builds_openai_model_with_base_url():
    tier = Tier(provider=Provider.VLLM, model="gemma-4-31b-it",
                endpoint="http://10.0.0.1:8020/v1", api_key_env="OSS_INFERENCE_API_KEY")
    os.environ["OSS_INFERENCE_API_KEY"] = "dummy-oss"
    handle = build_handle(tier, StepClientKind.AGENT)
    assert type(handle).__name__ in _openai_model_types()
    assert str(handle.base_url).rstrip("/") == "http://10.0.0.1:8020/v1"
    assert handle.model_name == "gemma-4-31b-it"


def test_factory_openai_agent_targets_openai_default():
    tier = Tier(provider=Provider.OPENAI, model="gpt-4.1", api_key_env="OPENAI_API_KEY")
    handle = build_handle(tier, StepClientKind.AGENT)
    assert type(handle).__name__ in _openai_model_types()
    assert "openai.com" in str(handle.base_url)


def test_factory_anthropic_agent_builds_anthropic_model():
    os.environ["ANTHROPIC_API_KEY"] = "anthropic-dummy"
    tier = Tier(provider=Provider.ANTHROPIC, model="claude-haiku-4-5", api_key_env="ANTHROPIC_API_KEY")
    handle = build_handle(tier, StepClientKind.AGENT)
    assert type(handle).__name__ == "AnthropicModel"


def test_factory_azure_agent_builds_model():
    tier = Tier(provider=Provider.AZURE, model="my-deploy",
                endpoint="https://example.openai.azure.com", api_version="2024-02-01",
                api_key_env="AZURE_OPENAI_API_KEY")
    os.environ["AZURE_OPENAI_API_KEY"] = "azure-dummy"
    handle = build_handle(tier, StepClientKind.AGENT)
    assert type(handle).__name__ in _openai_model_types()


def test_factory_gemini_agent_builds_model():
    tier = Tier(provider=Provider.GEMINI, model="gemini-2.5-flash", api_key_env="GEMINI_API_KEY")
    os.environ["GEMINI_API_KEY"] = "gemini-dummy"
    try:
        handle = build_handle(tier, StepClientKind.AGENT)
    except (RuntimeError, ImportError) as exc:  # SDK genuinely unavailable
        pytest.skip(f"gemini SDK unavailable: {exc}")
    assert type(handle).__name__ in ("GoogleModel", "GeminiModel")


def test_factory_raw_openai_client_carries_api_key_and_base_url():
    os.environ["OSS_INFERENCE_API_KEY"] = "dummy-oss"
    tier = Tier(provider=Provider.VLLM, model="gemma-4-31b-it",
                endpoint="http://10.0.0.1:8020/v1", api_key_env="OSS_INFERENCE_API_KEY")
    client = build_handle(tier, StepClientKind.RAW_OPENAI)
    assert type(client).__name__ == "AsyncOpenAI"
    assert str(client.base_url).rstrip("/") == "http://10.0.0.1:8020/v1"
    assert client.api_key == "dummy-oss"


def test_factory_translategemma_builds_descriptor():
    tier = Tier(provider=Provider.TRANSLATEGEMMA, model="translategemma-27b-base",
                endpoint="http://localhost:18002/v1", api_style=ApiStyle.TEXT_COMPLETION)
    desc = build_handle(tier, StepClientKind.TRANSLATEGEMMA)
    assert isinstance(desc, TGDescriptor)
    assert desc.completions_url == "http://localhost:18002/v1/completions"
    assert desc.model_id == "translategemma-27b-base"


# ── legality enforcement ──────────────────────────────────────────────────────

def test_factory_rejects_anthropic_for_raw_openai():
    tier = Tier(provider=Provider.ANTHROPIC, model="claude-haiku-4-5")
    with pytest.raises(ValueError):
        build_handle(tier, StepClientKind.RAW_OPENAI)


def test_factory_rejects_translategemma_for_agent():
    tier = Tier(provider=Provider.TRANSLATEGEMMA, model="tg", endpoint="http://x/v1")
    with pytest.raises(ValueError):
        build_handle(tier, StepClientKind.AGENT)


def test_factory_rejects_openai_for_translategemma_kind():
    tier = Tier(provider=Provider.OPENAI, model="gpt-4.1")
    with pytest.raises(ValueError):
        build_handle(tier, StepClientKind.TRANSLATEGEMMA)


# ── (D) vLLM tier without an endpoint must RAISE (not silently target OpenAI) ──

def test_factory_raw_openai_vllm_without_endpoint_raises():
    """A vLLM RAW_OPENAI tier with no endpoint RAISES rather than silently building
    an OpenAI-default client — so moderation/non_meaningful stay FAIL-OPEN when OSS
    is unconfigured, instead of flipping to fail-closed via OpenAI (MUST-FIX D)."""
    tier = Tier(provider=Provider.VLLM, model="gemma", endpoint=None,
                api_key_env="OSS_INFERENCE_API_KEY")
    with pytest.raises(ValueError):
        build_handle(tier, StepClientKind.RAW_OPENAI)


def test_factory_vllm_agent_without_endpoint_raises():
    """Same guard on the AGENT vLLM builder."""
    tier = Tier(provider=Provider.VLLM, model="gemma", endpoint="")
    with pytest.raises(ValueError):
        build_handle(tier, StepClientKind.AGENT)


# ── materialize ───────────────────────────────────────────────────────────────

def test_materialize_preserves_order_and_timeout():
    tiers = [
        Tier(provider=Provider.VLLM, model="gemma", endpoint="http://oss:8020/v1", timeout_ms=8000),
        Tier(provider=Provider.OPENAI, model="gpt-4.1", timeout_ms=20000),
    ]
    mts = materialize(StepClientKind.AGENT, tiers)
    assert len(mts) == 2
    assert isinstance(mts[0], MaterializedTier)
    assert mts[0].timeout == 8.0 and mts[1].timeout == 20.0
    assert mts[0].model is mts[0].handle
    assert mts[0].provider == "vllm" and mts[1].provider == "openai"


# ── voice Step enum: non_meaningful present, suggestions absent ────────────────

def test_step_enum_has_non_meaningful_and_no_suggestions():
    values = {s.value for s in Step}
    assert "non_meaningful" in values
    assert "suggestions" not in values
    # MODERATION + NON_MEANINGFUL materialize to RAW_OPENAI (voice deviation).
    assert resolver.STEP_CLIENT_KIND[Step.MODERATION] is StepClientKind.RAW_OPENAI
    assert resolver.STEP_CLIENT_KIND[Step.NON_MEANINGFUL] is StepClientKind.RAW_OPENAI
    assert resolver.STEP_CLIENT_KIND[Step.AGENT] is StepClientKind.AGENT


# ── shim identity ─────────────────────────────────────────────────────────────

def test_shim_managed_only_when_oss_unconfigured(monkeypatch):
    for k in ("OSS_INFERENCE_ENDPOINT_URL", "OSS_PIPELINE_PCT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL_NAME", "gpt-4")
    cfg = synthesize_from_env()
    assert [(p.name, p.weight) for p in cfg.profiles] == [("managed", 100)]
    managed = cfg.by_name("managed")
    agent = cfg.step_config(managed, Step.AGENT).tiers[0]
    assert agent.provider is Provider.OPENAI and agent.model == "gpt-4"
    assert agent.api_key_env == "OPENAI_API_KEY" and agent.endpoint is None
    assert agent.timeout_ms == 20000  # FALLBACK_MANAGED_TIMEOUT_MS default
    # non_meaningful + post_translation are profile-invariant (defaults).
    assert Step.NON_MEANINGFUL in cfg.defaults
    assert Step.POST_TRANSLATION in cfg.defaults


def test_shim_two_profiles_when_oss_configured(monkeypatch):
    monkeypatch.setenv("OSS_INFERENCE_ENDPOINT_URL", "http://oss:8020/v1")
    monkeypatch.setenv("OSS_LLM_MODEL_NAME", "gemma-4-31b-it")
    monkeypatch.setenv("OSS_PIPELINE_PCT", "80")
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL_NAME", "gpt-4.1")
    cfg = synthesize_from_env()
    assert {p.name: p.weight for p in cfg.profiles} == {"oss": 80, "managed": 20}
    oss = cfg.by_name("oss")
    # OSS agent step mirrors attempt_chain: [oss, managed]
    oss_agent = cfg.step_config(oss, Step.AGENT).tiers
    assert [t.provider for t in oss_agent] == [Provider.VLLM, Provider.OPENAI]
    assert oss_agent[0].endpoint == "http://oss:8020/v1"
    assert oss_agent[0].model == "gemma-4-31b-it"
    assert oss_agent[0].timeout_ms == 8000  # FALLBACK_CHAT_OSS_TIMEOUT_MS default
    # OSS moderation is RAW_OPENAI: [oss vLLM pretrans model, managed pretrans model].
    oss_mod = cfg.step_config(oss, Step.MODERATION).tiers
    assert [t.provider for t in oss_mod] == [Provider.VLLM, Provider.OPENAI]
    assert oss_mod[0].timeout_ms == 5000  # FALLBACK_MODERATION_OSS_TIMEOUT_MS default


def test_shim_non_meaningful_provider_governed_by_env(monkeypatch):
    monkeypatch.setenv("OSS_INFERENCE_ENDPOINT_URL", "http://oss:8020/v1")
    monkeypatch.setenv("OSS_PRETRANSLATION_MODEL", "gemma-oss")
    # default VOICE_NON_MEANINGFUL_PROVIDER=vllm -> OSS pretranslation model.
    monkeypatch.delenv("VOICE_NON_MEANINGFUL_PROVIDER", raising=False)
    cfg = synthesize_from_env()
    nm = cfg.defaults[Step.NON_MEANINGFUL].tiers
    assert len(nm) == 1
    assert nm[0].provider is Provider.VLLM and nm[0].model == "gemma-oss"
    # openai override -> managed OpenAI pretranslation model.
    monkeypatch.setenv("VOICE_NON_MEANINGFUL_PROVIDER", "openai")
    monkeypatch.setenv("PRETRANSLATION_PROVIDER", "openai")
    monkeypatch.setenv("OPENAI_PRETRANSLATION_MODEL", "gpt-5.1")
    cfg2 = synthesize_from_env()
    nm2 = cfg2.defaults[Step.NON_MEANINGFUL].tiers[0]
    assert nm2.provider is Provider.OPENAI and nm2.model == "gpt-5.1"


def test_shim_pretranslation_and_post_translation(monkeypatch):
    monkeypatch.delenv("OSS_INFERENCE_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.delenv("PRETRANSLATION_PROVIDER", raising=False)
    monkeypatch.delenv("PRETRANSLATION_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_PRETRANSLATION_MODEL", raising=False)
    monkeypatch.setenv("TRANSLATEGEMMA_27B_BASE_ENDPOINT", "http://localhost:18002/v1")
    cfg = synthesize_from_env()
    managed = cfg.by_name("managed")
    pre = cfg.step_config(managed, Step.PRE_TRANSLATION).tiers[0]
    # voice pretranslation default (openai provider) = gpt-5.1
    assert pre.provider is Provider.OPENAI and pre.model == "gpt-5.1"
    post = cfg.defaults[Step.POST_TRANSLATION].tiers[0]
    assert post.provider is Provider.TRANSLATEGEMMA
    assert post.api_style is ApiStyle.TEXT_COMPLETION
    assert post.endpoint == "http://localhost:18002/v1"
    assert post.model == "translategemma-27b-base"


def test_shim_agent_resolves_to_env_managed_tier(monkeypatch):
    """Resolver's AGENT primary reflects the env-synthesized managed tier
    (provider + model come from LLM_PROVIDER / LLM_MODEL_NAME)."""
    monkeypatch.delenv("OSS_INFERENCE_ENDPOINT_URL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    monkeypatch.setenv("LLM_MODEL_NAME", "gpt-4.1")
    runtime.configure(run_self_check=False)
    mt = resolver.primary_tier(Step.AGENT, "legacy")
    assert mt.provider == "openai"
    assert mt.model_name == "gpt-4.1"
    assert mt.handle is not None


# ── resolver ──────────────────────────────────────────────────────────────────

def test_resolver_returns_non_empty_chain():
    runtime.configure(run_self_check=False)
    chain = resolver.resolve_chain(Step.AGENT, "legacy")
    assert len(chain) >= 1
    assert chain[0].handle is not None
    # post-translation resolves to a TG descriptor
    post = resolver.resolve_chain(Step.POST_TRANSLATION, "legacy")
    assert isinstance(post[0].handle, TGDescriptor)
    # moderation resolves to a RAW_OPENAI client
    mod = resolver.resolve_chain(Step.MODERATION, "legacy")
    assert type(mod[0].handle).__name__ == "AsyncOpenAI"


def test_resolver_falls_back_to_managed_when_oss_profile_absent():
    runtime.configure(run_self_check=False)
    chain = resolver.resolve_chain(Step.AGENT, "oss")
    assert len(chain) >= 1


# ── self-check (resolvability, non-fatal) ─────────────────────────────────────

def test_self_check_is_non_fatal_on_unresolvable_step(monkeypatch):
    """The P4 self-check logs+warns on a step that fails to resolve; it must NOT
    raise (a materialize edge case must never block startup)."""
    runtime.configure(run_self_check=False)

    def _boom(step, variant="legacy"):
        raise RuntimeError("cannot build handle in this env")

    monkeypatch.setattr(resolver, "primary_tier", _boom)
    # No exception — self_check swallows resolve failures into a warning log.
    runtime.self_check()


def test_configure_runs_self_check_without_raising():
    """Startup config load + self-check must be robust and return a valid config."""
    cfg = runtime.configure()  # run_self_check defaults True
    assert cfg is not None and len(cfg.profiles) >= 1


# ── (E) startup config validation: RAW_OPENAI steps reject anthropic/gemini ────

def test_validate_config_rejects_anthropic_raw_step():
    """A RAW_OPENAI step (pre_translation/moderation/non_meaningful) whose tier is
    anthropic/gemini is rejected at startup — fail fast, not per-request (MUST-FIX E)."""
    from app.llm_core.config_model import NamedProfile, PipelineConfig, StepConfig

    bad = PipelineConfig(profiles=[
        NamedProfile(name="managed", weight=100, steps={
            Step.MODERATION: StepConfig(tiers=[Tier(provider=Provider.ANTHROPIC, model="claude")]),
        }),
    ])
    with pytest.raises(runtime.PipelineConfigError):
        runtime.validate_config(bad)


def test_validate_config_rejects_gemini_in_defaults():
    from app.llm_core.config_model import NamedProfile, PipelineConfig, StepConfig

    bad = PipelineConfig(
        profiles=[NamedProfile(name="managed", weight=100,
                               steps={Step.AGENT: StepConfig(tiers=[Tier(provider=Provider.OPENAI, model="gpt")])})],
        defaults={Step.NON_MEANINGFUL: StepConfig(tiers=[Tier(provider=Provider.GEMINI, model="gemini")])},
    )
    with pytest.raises(runtime.PipelineConfigError):
        runtime.validate_config(bad)


def test_validate_config_accepts_supported_raw_providers():
    """openai / vllm / azure-openai RAW tiers pass; anthropic on an AGENT step is
    fine (only RAW steps are restricted)."""
    from app.llm_core.config_model import NamedProfile, PipelineConfig, StepConfig

    ok = PipelineConfig(profiles=[
        NamedProfile(name="managed", weight=100, steps={
            Step.AGENT: StepConfig(tiers=[Tier(provider=Provider.ANTHROPIC, model="claude")]),
            Step.MODERATION: StepConfig(tiers=[Tier(provider=Provider.OPENAI, model="gpt")]),
            Step.NON_MEANINGFUL: StepConfig(
                tiers=[Tier(provider=Provider.VLLM, model="gemma", endpoint="http://oss:8020/v1")]),
            Step.PRE_TRANSLATION: StepConfig(
                tiers=[Tier(provider=Provider.AZURE, model="dep", endpoint="https://x.openai.azure.com",
                            api_version="2024-02-01", api_key_env="AZURE_OPENAI_API_KEY")]),
        }),
    ])
    runtime.validate_config(ok)  # must not raise


# ── ENABLE: AGENT-step ConcurrencyGate synthesized from the explicit env ───────

def _agent_step(cfg):
    for prof in cfg.profiles:
        if Step.AGENT in prof.steps:
            return prof.steps[Step.AGENT]
    return cfg.defaults[Step.AGENT]


def test_shim_attaches_concurrency_gate_when_metrics_url_set(monkeypatch):
    monkeypatch.setenv("AGENT_CONCURRENCY_METRICS_URL", "http://10.185.25.197:8020/metrics")
    monkeypatch.setenv("CONCURRENCY_MAX", "7")
    cfg = synthesize_from_env()
    gate = _agent_step(cfg).triggers.concurrency_gate
    assert gate is not None
    assert gate.metrics_url == "http://10.185.25.197:8020/metrics"
    assert gate.max_concurrency == 7


def test_shim_no_concurrency_gate_when_metrics_url_unset(monkeypatch):
    monkeypatch.delenv("AGENT_CONCURRENCY_METRICS_URL", raising=False)
    cfg = synthesize_from_env()
    for prof in cfg.profiles:
        if Step.AGENT in prof.steps:
            assert prof.steps[Step.AGENT].triggers.concurrency_gate is None
