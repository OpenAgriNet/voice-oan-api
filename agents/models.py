import os
from openai import AsyncOpenAI
from openai.resources.chat.completions import AsyncCompletions
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from dotenv import load_dotenv

load_dotenv()


class VLLMCompletions(AsyncCompletions):
    """Strips response_format from streaming calls.

    Workaround for vLLM bug: streaming + response_format + multi-turn = empty response.
    The system prompt already instructs the model to produce JSON.
    """

    async def create(self, **kwargs):
        if kwargs.get('stream'):
            kwargs.pop('response_format', None)
        return await super().create(**kwargs)


_client = AsyncOpenAI(
    base_url=os.getenv('VLLM_AGRINET_MODEL_URL'),
    api_key="not-needed",
)
_client.chat.completions = VLLMCompletions(_client)

# Agrinet Model
LLM_AGRINET_MODEL = OpenAIChatModel(
    os.getenv('LLM_AGRINET_MODEL_NAME', 'agrinet-model'),
    provider=OpenAIProvider(openai_client=_client),
)