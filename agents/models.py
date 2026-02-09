import os
from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.models.openai import OpenAIResponsesModelSettings
from dotenv import load_dotenv

load_dotenv()
# Agrinet Model
LLM_AGRINET_MODEL = OpenAIChatModel(
    os.getenv('LLM_AGRINET_MODEL_NAME', 'agrinet-model'),
    provider=OpenAIProvider(
        base_url=os.getenv('VLLM_AGRINET_MODEL_URL'),
        api_key="not-needed",
    ),
)