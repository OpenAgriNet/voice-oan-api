from pathlib import Path

from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel

from agents.model_registry import ModelRegistry, _BUILDERS


def test_registry_supports_only_three_provider_styles(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("KEY", "test-key")
    path = tmp_path / "models.yaml"
    path.write_text(
        """
models:
  azure_classic:
    kind: azure-openai
    deployment_name: gpt-4.1
    endpoint: https://classic.invalid
    api_key: ${KEY}
    api_version: 2025-01-01-preview
  azure_mini:
    kind: azure-openai
    api: responses
    model_name: gpt-5.4-mini
    settings:
      openai_reasoning_effort: none
    base_url: https://mini.invalid/openai/v1
    api_key: ${KEY}
  openai_compatible:
    kind: openai
    model_name: remote-model
    base_url: https://remote.invalid/v1
    api_key: ${KEY}
  local_gemma:
    kind: vllm
    model_name: gemma
    base_url: http://localhost:8867/v1
    fallback:
      to: azure_mini
      on_error: [ModelHTTPError, TimeoutError]
use_cases:
  voice:
    default_alias: azure_mini
    aliases: [local_gemma, azure_mini]
    proportions: [70, 30]
"""
    )
    registry = ModelRegistry(path)

    assert set(_BUILDERS) == {"azure-openai", "openai", "vllm"}
    assert isinstance(registry.get_model("azure_classic"), OpenAIChatModel)
    assert isinstance(registry.get_model("azure_mini"), OpenAIResponsesModel)
    assert isinstance(registry.get_model("openai_compatible"), OpenAIChatModel)
    assert isinstance(registry.get_model("local_gemma"), OpenAIChatModel)
    assert registry.get_model("azure_mini").settings["openai_reasoning_effort"] == "none"
    registry.validate("voice")
    assert registry.aliases("voice") == ["local_gemma", "azure_mini"]
    assert registry.proportions("voice") == [70, 30]
    assert registry.default_alias("voice") == "azure_mini"
    assert registry.fallback("local_gemma") == "azure_mini"


def test_voice_config_routes_70_30_with_mini_fallback(monkeypatch):
    monkeypatch.setenv("VOICE_PROPORTION_GEMMA_VLLM", "70")
    monkeypatch.setenv("VOICE_PROPORTION_AZURE_GPT54_MINI", "30")
    registry = ModelRegistry()

    assert registry.aliases("voice") == ["gemma_vllm", "azure_gpt54_mini"]
    assert registry.proportions("voice") == [70, 30]
    assert registry.default_alias("voice") == "azure_gpt54_mini"
    assert registry.fallback("gemma_vllm") == "azure_gpt54_mini"


def test_gemma_fallback_covers_transport_and_model_failures():
    import httpx
    from openai import APIConnectionError
    from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior

    errors = ModelRegistry().fallback_errors("gemma_vllm")
    for error in [TimeoutError(), httpx.ReadError("disconnected"),
                  APIConnectionError(request=httpx.Request("POST", "http://test/v1")),
                  ModelHTTPError(503, "gemma"), UnexpectedModelBehavior("invalid output")]:
        assert isinstance(error, errors)
    assert not isinstance(ValueError("application bug"), errors)
