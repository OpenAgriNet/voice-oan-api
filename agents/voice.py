from pydantic_ai import Agent, RunContext
from app.core.languages import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
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
    language: str = Field(default=DEFAULT_LANGUAGE, description=f"ISO 639-1 code of the language this response is written in. Must be one of: {', '.join(SUPPORTED_LANGUAGES)}. Must match the language the user wrote in.")
    audio: str = Field(default=None, description="The audio content of the response. This is the text that will be converted to audio by the TTS engine.", min_length=1)
    end_interaction: bool = Field(default=False, description="Set to true ONLY when the user explicitly indicates they have no more questions. Defaults to false.")


voice_agent = Agent(
    model=LLM_AGRINET_MODEL,
    name="Voice Agent",
    output_type=NativeOutput(VoiceOutput, strict=False),
    instrument=None,
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
    # One prompt for every language. The model detects the language from the user's
    # own words and mirrors it, reporting the choice back in VoiceOutput.language.
    # Nothing here depends on the client's X-Language header any more.
    return get_prompt(
        'voice',
        context={'today_date': get_today_date_str()},
    )
