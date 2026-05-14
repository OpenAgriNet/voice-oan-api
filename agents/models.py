import os
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI

load_dotenv()


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


# Get configurations from environment variables
LLM_PROVIDER = (
    os.getenv("LLM_PROVIDER")
    or os.getenv("LLM_AGRINET_PROVIDER")
    or "vllm"
).lower()
LLM_MODEL_NAME = os.getenv("LLM_AGRINET_MODEL_NAME") or os.getenv("LLM_MODEL_NAME")

if LLM_PROVIDER == 'vllm':
    inference_url = _vllm_openai_base_url()
    inference_api_key = os.getenv("INFERENCE_API_KEY")
    LLM_MODEL = OpenAIModel(
        LLM_MODEL_NAME,
        provider=OpenAIProvider(
            base_url=inference_url,
            api_key=inference_api_key if inference_api_key else "not-required",
        ),
    )
elif LLM_PROVIDER == 'openai':
    LLM_MODEL = OpenAIModel(
        LLM_MODEL_NAME,
        provider=OpenAIProvider(
            api_key=os.getenv('OPENAI_API_KEY'),
        ),
    )
elif LLM_PROVIDER == 'azure-openai':
    azure_endpoint = os.getenv('AZURE_OPENAI_ENDPOINT')
    azure_api_key = os.getenv('AZURE_OPENAI_API_KEY')
    azure_api_version = os.getenv('AZURE_OPENAI_API_VERSION')
    azure_deployment_name = (
        os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME")
        or os.getenv("LLM_AGRINET_MODEL_NAME")
        or os.getenv("LLM_MODEL_NAME")
    )
    
    if not azure_endpoint:
        raise ValueError("AZURE_OPENAI_ENDPOINT environment variable is required")
    if not azure_api_key:
        raise ValueError("AZURE_OPENAI_API_KEY environment variable is required")
    if not azure_api_version:
        raise ValueError("AZURE_OPENAI_API_VERSION environment variable is required")
    if not azure_deployment_name:
        raise ValueError(
            "Azure deployment name is required: set AZURE_OPENAI_DEPLOYMENT_NAME "
            "(or LLM_AGRINET_MODEL_NAME / LLM_MODEL_NAME)"
        )
    
    azure_client = AsyncAzureOpenAI(
        azure_endpoint=azure_endpoint.rstrip('/'),
        api_version=azure_api_version,
        api_key=azure_api_key,
    )
    
    LLM_MODEL = OpenAIModel(
        azure_deployment_name,
        provider=OpenAIProvider(openai_client=azure_client),
    )
else:
    raise ValueError(f"Invalid LLM_PROVIDER: {LLM_PROVIDER}. Must be one of: 'vllm', 'openai', 'azure-openai'")