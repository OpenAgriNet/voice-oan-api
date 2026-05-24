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
    """Long-lived AsyncClient with the boundary-capture event hook attached."""
    return httpx.AsyncClient(event_hooks={"request": [_capture_request_hook]})


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


# --- OSS pipeline (open-source models via vLLM) ---------------------------------
# Built additively alongside the legacy LLM_MODEL so a per-request sticky split can
# route a configurable % of voice sessions to the OSS path (vLLM gemma agent +
# vLLM gemma pretranslation; post-translation TranslateGemma is unchanged).
#
# This block must never raise at import: if OSS env is absent, OSS_LLM_MODEL stays
# None and get_model_for_variant() transparently falls back to the legacy model, so
# legacy behaviour is byte-identical when the split is disabled (OSS_PIPELINE_PCT=0).
OSS_LLM_MODEL_NAME = os.getenv('OSS_LLM_MODEL_NAME', 'gemma-4-31b-it')
OSS_INFERENCE_ENDPOINT_URL = os.getenv('OSS_INFERENCE_ENDPOINT_URL')
OSS_INFERENCE_API_KEY = os.getenv('OSS_INFERENCE_API_KEY', 'dummy')

OSS_LLM_MODEL = None
if OSS_INFERENCE_ENDPOINT_URL:
    try:
        OSS_LLM_MODEL = _build_openai_compatible_model(
            OSS_LLM_MODEL_NAME,
            base_url=OSS_INFERENCE_ENDPOINT_URL,
            api_key=OSS_INFERENCE_API_KEY,
        )
    except Exception:  # pragma: no cover - never break startup on OSS misconfig
        OSS_LLM_MODEL = None


def oss_model_available() -> bool:
    """True when an OSS vLLM model object was successfully constructed."""
    return OSS_LLM_MODEL is not None


def get_model_for_variant(variant: str):
    """Return the pydantic-ai model object for a resolved pipeline variant.

    'oss' -> the vLLM model when configured, else the legacy model (fail-safe).
    anything else -> the legacy startup model (unchanged behaviour).
    """
    if variant == 'oss' and OSS_LLM_MODEL is not None:
        return OSS_LLM_MODEL
    return LLM_MODEL


def provider_for_variant(variant: str) -> str:
    """Effective provider for a resolved variant: OSS is OpenAI-compatible (vLLM)."""
    if variant == 'oss' and OSS_LLM_MODEL is not None:
        return 'vllm'
    return LLM_PROVIDER
