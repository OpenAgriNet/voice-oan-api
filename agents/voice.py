from typing import Literal, Optional
from pydantic_ai import Agent, RunContext
from app.core.languages import is_supported, resolve_render_language, DEFAULT_LANGUAGE
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
        timeout=30,
    )
)

@voice_agent.instructions
def get_voice_system_prompt(ctx: RunContext[FarmerContext]):
    # The client specifies the conversation language via the X-Language header
    # (deps.lang_code). When it is a supported code, respond in that language and
    # skip the language gate. When it is missing ("none"/unknown), fall back to the
    # gated Hindi prompt that asks the user to pick English or Hindi.
    requested = ctx.deps.lang_code
    if is_supported(requested):
        # Use the requested language if it has a prompt file, else fall back to Hindi.
        render_lang = resolve_render_language(requested)
        if render_lang != requested:
            logger.warning(
                f"No prompt file for language '{requested}'. Responding in Hindi."
            )
        ask_language_gate = False
    else:
        # No explicit language preference -> ask the user (gate), default to Hindi.
        render_lang = DEFAULT_LANGUAGE
        ask_language_gate = True

    prompt_file = f"voice_{render_lang}"
    return get_prompt(
        prompt_file,
        context={
            'today_date': get_today_date_str(),
            'ask_language_gate': ask_language_gate,
        },
    )
