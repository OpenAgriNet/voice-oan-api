import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from dotenv import load_dotenv
from openai import APIError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior, UsageLimitExceeded
from pydantic_ai.models.fallback import FallbackModel
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.models.wrapper import WrapperModel
from pydantic_ai.providers.azure import AzureProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings

from helpers.utils import get_logger

load_dotenv()

logger = get_logger(__name__)

AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_FALLBACK_DEPLOYMENT = os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-4.1")

# Per-request model errors. UsageLimitExceeded is enforced at Agent.run() level
# (see app.services.voice._run_voice_agent) and is not raised inside model.request().
_FALLBACK_ON = (ModelHTTPError, APIError, UnexpectedModelBehavior, UsageLimitExceeded)


def _openai_compatible_base_url(url: str | None) -> str | None:
    """Ensure OpenAI-compatible servers get a base URL ending with /v1."""
    if not url:
        return None
    u = url.strip().rstrip("/")
    if u.endswith("/v1"):
        return u
    return f"{u}/v1"


def _vllm_openai_base_url() -> str | None:
    """
    Base URL for OpenAI-compatible chat/completions.

    VLLM_OPENAI_BASE_URL overrides everything when set.

    Otherwise prefer deriving from VLLM_AGRINET_MODEL_URL (typically .../v1/models)
    so the chat host matches the server that lists LLM_AGRINET_MODEL_NAME. Fall back
    to INFERENCE_ENDPOINT_URL.
    """
    override = os.getenv("VLLM_OPENAI_BASE_URL")
    if override:
        return _openai_compatible_base_url(override)
    listed = os.getenv("VLLM_AGRINET_MODEL_URL")
    if listed:
        u = listed.strip().rstrip("/")
        if u.endswith("/v1/models"):
            u = u[: -len("/models")]
        elif u.endswith("/models"):
            u = u[: -len("/models")]
        return _openai_compatible_base_url(u)
    return _openai_compatible_base_url(os.getenv("INFERENCE_ENDPOINT_URL"))


def _azure_configured() -> bool:
    return bool(os.getenv("AZURE_OPENAI_ENDPOINT") and os.getenv("AZURE_OPENAI_API_KEY"))


def _sanitize_settings_for_azure(model_settings: ModelSettings | None) -> ModelSettings | None:
    """Strip vLLM-only extra_body fields before calling Azure OpenAI."""
    if not model_settings:
        return None
    sanitized = dict(model_settings)
    sanitized.pop("extra_body", None)
    return ModelSettings(**sanitized) if sanitized else None


@dataclass(init=False)
class _AzureSanitizedModel(WrapperModel):
    """Azure fallback wrapper that omits vLLM-specific request settings."""

    async def request(self, messages, model_settings, model_request_parameters, run_context=None):
        return await self.wrapped.request(
            messages,
            _sanitize_settings_for_azure(model_settings),
            model_request_parameters,
            run_context,
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncIterator[StreamedResponse]:
        async with self.wrapped.request_stream(
            messages,
            _sanitize_settings_for_azure(model_settings),
            model_request_parameters,
            run_context,
        ) as response_stream:
            yield response_stream


def _make_vllm_model(model_name: str | None) -> OpenAIModel:
    inference_url = _vllm_openai_base_url()
    inference_api_key = os.getenv("INFERENCE_API_KEY")
    return OpenAIModel(
        model_name,
        provider=OpenAIProvider(
            base_url=inference_url,
            api_key=inference_api_key if inference_api_key else "not-required",
        ),
    )


def _make_azure_model() -> OpenAIModel:
    """Azure OpenAI model used only as error fallback (default deployment: gpt-4.1)."""
    azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
    azure_api_key = os.getenv("AZURE_OPENAI_API_KEY")

    if not azure_endpoint:
        raise ValueError("AZURE_OPENAI_ENDPOINT environment variable is required")
    if not azure_api_key:
        raise ValueError("AZURE_OPENAI_API_KEY environment variable is required")

    return OpenAIModel(
        AZURE_FALLBACK_DEPLOYMENT,
        provider=AzureProvider(
            azure_endpoint=azure_endpoint.rstrip("/"),
            api_version=AZURE_OPENAI_API_VERSION,
            api_key=azure_api_key,
        ),
    )


def _with_azure_fallback(primary: OpenAIModel) -> FallbackModel | OpenAIModel:
    if not _azure_configured():
        logger.warning(
            "Azure OpenAI fallback disabled: set AZURE_OPENAI_ENDPOINT and AZURE_OPENAI_API_KEY"
        )
        return primary
    logger.info(
        "LLM: primary=vLLM (%s), fallback=Azure (%s) on %s",
        primary.model_name,
        AZURE_FALLBACK_DEPLOYMENT,
        ", ".join(e.__name__ for e in _FALLBACK_ON),
    )
    return FallbackModel(
        primary,
        _AzureSanitizedModel(_make_azure_model()),
        fallback_on=_FALLBACK_ON,
    )


# Default: vLLM for every request; Azure gpt-4.1 only when vLLM errors.
LLM_PROVIDER = (
    os.getenv("LLM_PROVIDER")
    or os.getenv("LLM_AGRINET_PROVIDER")
    or "vllm"
).lower()
LLM_AGRINET_MODEL_NAME = os.getenv("LLM_AGRINET_MODEL_NAME")
LLM_MODEL_NAME = LLM_AGRINET_MODEL_NAME or os.getenv("LLM_MODEL_NAME")

# Standalone Azure model for agent-level fallback (e.g. UsageLimitExceeded retry).
AZURE_LLM_MODEL = (
    _AzureSanitizedModel(_make_azure_model()) if _azure_configured() else None
)

if LLM_PROVIDER == "vllm":
    if not LLM_AGRINET_MODEL_NAME:
        raise ValueError(
            "LLM_AGRINET_MODEL_NAME is required when LLM_PROVIDER=vllm "
            "(e.g. LLM_AGRINET_MODEL_NAME=agrinet-model)"
        )
    LLM_MODEL = _with_azure_fallback(_make_vllm_model(LLM_AGRINET_MODEL_NAME))
elif LLM_PROVIDER == "openai":
    LLM_MODEL = OpenAIModel(
        LLM_MODEL_NAME,
        provider=OpenAIProvider(
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
    )
elif LLM_PROVIDER == "azure-openai":
    logger.warning(
        "LLM_PROVIDER=azure-openai uses Azure only (no vLLM). "
        "Use LLM_PROVIDER=vllm (default) for vLLM primary with Azure fallback on error."
    )
    LLM_MODEL = _make_azure_model()
else:
    raise ValueError(
        f"Invalid LLM_PROVIDER: {LLM_PROVIDER}. Must be one of: 'vllm', 'openai', 'azure-openai'"
    )
