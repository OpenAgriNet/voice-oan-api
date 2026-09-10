from pydantic_ai import Agent, RunContext
from app.core.languages import (
    DEFAULT_LANGUAGE,
    SUPPORTED_LANGUAGES,
    get_language,
)
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
    """Assistant's response to the user's query."""
    # Declared first so it is decoded early in the streamed JSON — the recording
    # disclaimer is picked from it before any audio is emitted (see app/services/voice.py).
    language: str = Field(
        default=DEFAULT_LANGUAGE,
        description=(
            "ISO 639-1 code used by the spoken response. Must equal the backend "
            "session lock when present; otherwise detect one of: "
            f"{', '.join(SUPPORTED_LANGUAGES)}."
        ),
    )
    lock_language: bool = Field(
        default=False,
        description="True only when this turn contains the first substantive user query and the detected language should be locked for the session.",
    )
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
        timeout=30,
    )
)

@voice_agent.instructions
def get_voice_system_prompt(ctx: RunContext[FarmerContext]):
    locked_language = ctx.deps.language_code
    locale = get_language(locked_language)
    return get_prompt(
        'voice',
        context={
            'today_date': get_today_date_str(),
            'locked_language': locked_language,
            'locked_language_name': locale.name,
            'supported_language_codes': ', '.join(SUPPORTED_LANGUAGES),
            'closing_message': locale.closing_message,
        },
    )
