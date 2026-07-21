import json
import logging
import os
from typing import Optional

import httpx
from openai import AsyncAzureOpenAI
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.openai import OpenAIProvider
from dotenv import load_dotenv

from app.model_boundary_capture import boundary_capture_enabled, capture_model_boundary_payload

load_dotenv()

logger = logging.getLogger(__name__)


# ── Model-boundary capture ────────────────────────────────────────────────────
# pydantic-ai 1.x no longer exposes the `_completions_create` override the old
# BoundaryCaptureOpenAIModel subclass hooked. Capture the outgoing chat payload
# at the HTTP layer instead, via an httpx request event-hook on a custom client
# passed to OpenAIProvider. Gated by MODEL_BOUNDARY_CAPTURE_ENABLED; best-effort,
# never raises (a failed capture must never drop a model request).
async def _capture_request_hook(request: httpx.Request) -> None:
    if not boundary_capture_enabled():
        return
    try:
        if not request.url.path.endswith("/chat/completions"):
            return
        raw = request.content
        if not raw:
            return
        body = json.loads(raw.decode("utf-8"))
        capture_model_boundary_payload(
            {
                "model_name": body.get("model"),
                "provider": "openai-compatible",
                "stream": bool(body.get("stream", False)),
                "tool_choice": body.get("tool_choice"),
                "url": str(request.url),
                "payload": body,
            }
        )
    except Exception as exc:  # pragma: no cover - capture is best-effort
        logger.debug("Model boundary capture hook failed: %s", exc)


def _capture_http_client() -> httpx.AsyncClient:
    """Long-lived AsyncClient with the boundary-capture event hook attached.

    Pin an explicit 600s read/write/pool timeout (5s connect) to match the
    OpenAI SDK default and pydantic-ai's cached_async_http_client. A bare
    AsyncClient inherits httpx's 5s default, which would abort long streaming
    agent runs (multi-second generation + tool round-trips) under load.
    """
    return httpx.AsyncClient(
        event_hooks={"request": [_capture_request_hook]},
        timeout=httpx.Timeout(600.0, connect=5.0),
    )


def _build_openai_compatible_model(
    model_name: str,
    *,
    base_url: Optional[str],
    api_key: Optional[str],
) -> OpenAIChatModel:
    return OpenAIChatModel(
        model_name,
        provider=OpenAIProvider(
            base_url=base_url,
            api_key=api_key,
            http_client=_capture_http_client(),
        ),
    )


# Get configurations from environment variables
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'openai').lower()
LLM_MODEL_NAME = os.getenv('LLM_MODEL_NAME')

# LLM_MODEL is the construction-time default model for every pydantic-ai agent
# (voice / signed-in voice). Every request passes an explicit ``model=`` resolved
# by ``app.llm_core`` (the unified config-driven pipeline is the only runtime
# path), so this default is only a fallback and is never used per-turn. The
# OSS/legacy split + get_model_for_variant/provider_for_variant/oss_model_available
# selectors were removed at P4 — model selection now lives entirely in
# app/llm_core (factory + resolver + weighted-profile split). The low-level
# builders below (_build_openai_compatible_model / the azure arm) are the shared
# handle constructors the factory reuses.
if LLM_PROVIDER == 'vllm':
    LLM_MODEL = _build_openai_compatible_model(
        LLM_MODEL_NAME,
        base_url=os.getenv('INFERENCE_ENDPOINT_URL'),
        api_key=os.getenv('INFERENCE_API_KEY'),
    )
elif LLM_PROVIDER == 'openai':
    LLM_MODEL = _build_openai_compatible_model(
        LLM_MODEL_NAME,
        base_url=None,
        api_key=os.getenv('OPENAI_API_KEY'),
    )
elif LLM_PROVIDER == 'anthropic':
    # AnthropicModel reads ANTHROPIC_API_KEY from the environment
    # and uses the Anthropic SDK under the hood.
    if not LLM_MODEL_NAME:
        raise ValueError("LLM_MODEL_NAME environment variable is required when using 'anthropic' provider")
    LLM_MODEL = AnthropicModel(LLM_MODEL_NAME)
elif LLM_PROVIDER == 'azure-openai':
    azure_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
    azure_api_key = os.getenv('AZURE_OPENAI_API_KEY')
    azure_api_version = os.getenv('AZURE_OPENAI_API_VERSION')
    azure_deployment_name = os.getenv('AZURE_OPENAI_DEPLOYMENT_NAME')

    if not azure_endpoint:
        raise ValueError("AZURE_OPENAI_ENDPOINT environment variable is required")
    if not azure_api_key:
        raise ValueError("AZURE_OPENAI_API_KEY environment variable is required")
    if not azure_api_version:
        raise ValueError("AZURE_OPENAI_API_VERSION environment variable is required")
    if not azure_deployment_name:
        raise ValueError("AZURE_OPENAI_DEPLOYMENT_NAME environment variable is required")

    azure_client = AsyncAzureOpenAI(
        azure_endpoint=azure_endpoint.rstrip('/'),
        api_version=azure_api_version,
        api_key=azure_api_key,
        http_client=_capture_http_client(),
    )

    LLM_MODEL = OpenAIChatModel(
        azure_deployment_name,
        provider=OpenAIProvider(openai_client=azure_client),
    )
else:
    raise ValueError(
        f"Invalid LLM_PROVIDER: {LLM_PROVIDER}. Must be one of: 'vllm', 'openai', 'azure-openai', 'anthropic'"
    )
