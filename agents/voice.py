from typing import Literal, Optional
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

    Never assume the user's language. Always ask first which language they want, then set this from their choice.
    """
    audio: str = Field(default=None, description="The audio content of the response. This is the text that will be converted to audio by the TTS engine.", min_length=1)
    end_interaction: bool = Field(default=False, description="Set to true ONLY when the user explicitly indicates they have no more questions. Defaults to false.")
    language: Optional[Literal["en", "hi" , "None"]] = Field(
        default="None",
        description="Ask the user which language they want for the conversation (English or Hindi), then set this from their answer: 'en' for English, 'hi' for Hindi. Leave null until they have chosen. Never assume.",
    )


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
    # Session default is Hindi at start; prompt is chosen by deps.lang_code (from client). Never assume language—always ask user first.
    target_lang = ctx.deps.lang_code if ctx.deps.lang_code else 'hi'
    if target_lang not in ['hi', 'en']:
        logger.warning(f"Invalid language code: {target_lang}. Defaulting to Hindi.")
        target_lang = 'hi'
    prompt_file = f"voice_{target_lang}"
    base_prompt = get_prompt(prompt_file, context={'today_date': get_today_date_str()})
    language_rule = (
        "\n\n**CRITICAL — LANGUAGE (DO THIS FIRST EVERY TURN): "
        "1) Look at the conversation history at the USER's own words. Has the user explicitly said they want English or Hindi (or equivalent)? "
        "2) If NO: Your ONLY response is to ask: 'Which language do you prefer to have the conversation in, English or Hindi?' (or in Hindi: 'आप बातचीत किस भाषा में करना पसंद करेंगे, अंग्रेज़ी या हिंदी?'). Do NOT call any tools. Do NOT answer their question. When asking this, set **\"language\": null** in your JSON. "
        "3) Ignore any 'Selected Language' in the request. Only the user's explicit words in the conversation count. "
        "4) Only once the user has said English or Hindi, set 'language' to \"en\" or \"hi\" and proceed. After that, respond in that language only for the rest of the conversation—every reply must be in the chosen language.**\n"
    )
    return language_rule + base_prompt
