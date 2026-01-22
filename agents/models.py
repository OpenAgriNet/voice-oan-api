import os
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from dotenv import load_dotenv

load_dotenv()

# Get configurations from environment variables
LLM_PROVIDER = os.getenv('LLM_PROVIDER', 'openai').lower()
LLM_MODEL_NAME = os.getenv('LLM_MODEL_NAME', 'gpt-4.1-nano')

if LLM_PROVIDER == 'openai':
    LLM_MODEL = (
        OpenAIChatModel(        
            LLM_MODEL_NAME,
            provider=OpenAIProvider(
                api_key=os.getenv('OPENAI_API_KEY'),
            ),
        )
    )


# GPT-OSS Models

if LLM_PROVIDER == 'vllm':
    LLM_MODEL = OpenAIChatModel(
        LLM_MODEL_NAME,
    provider=OpenAIProvider(
        base_url=os.getenv('VLLM_BASE_URL'), 
        api_key="not-needed"
    ),
    )
else:
    raise ValueError(f"Invalid LLM_PROVIDER: {LLM_PROVIDER}. Must be one of: 'openai', 'vllm'")