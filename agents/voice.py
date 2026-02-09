from pydantic_ai import Agent, RunContext
from helpers.utils import get_prompt, get_today_date_str
from agents.models import LLM_AGRINET_MODEL
from agents.tools import TOOLS
from pydantic_ai.settings import ModelSettings
from agents.deps import FarmerContext
from pydantic import BaseModel, Field
from pydantic_ai import NativeOutput
import logging

logger = logging.getLogger(__name__)

class VoiceOutput(BaseModel):
    """Assistant's response to the user's query.
    
    Attributes:
        audio: The audio content of the response. This is the text that will be converted to audio by the TTS engine.
        end_interaction: Set to true ONLY when the user explicitly indicates they have no more questions. Default is false.
    """
    audio: str = Field(default=None, description="The audio content of the response. This is the text that will be converted to audio by the TTS engine.", min_length=1)
    end_interaction: bool = Field(default=False, description="Set to true ONLY when the user explicitly indicates they have no more questions. Defaults to false.")


voice_agent = Agent(
    model=LLM_AGRINET_MODEL,
    name="Voice Agent",
    output_type=NativeOutput(VoiceOutput, strict=False),
    instrument=True,
    deps_type=FarmerContext,
    retries=3,
    output_retries=3,
    tools=TOOLS,
    end_strategy='exhaustive',
    model_settings=ModelSettings(
        # temperature=0.7,
        # top_p=0.95,
        # top_k=50,
        parallel_tool_calls=True,
        openai_reasoning_effort='low',
        openai_send_reasoning_ids=False,
        timeout=30,
    )
)

@voice_agent.instructions
def get_voice_system_prompt(ctx: RunContext[FarmerContext]):
    # Choose prompt based on target language (lang_code from deps)
    target_lang = ctx.deps.lang_code if ctx.deps.lang_code else 'hi'
    if target_lang not in ['hi', 'en']:
        logger.warning(f"Invalid language code: {target_lang}. Defaulting to Hindi.")
        target_lang = 'hi'
    prompt_file = f"voice_{target_lang}"
    return get_prompt(prompt_file, context={'today_date': get_today_date_str()})
