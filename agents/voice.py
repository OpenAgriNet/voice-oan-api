import re
from pydantic_ai import Agent, ModelRetry, RunContext
from app.core.languages import DEFAULT_LANGUAGE
from helpers.utils import get_prompt, get_today_date_str
from agents.models import LLM_AGRINET_MODEL
from agents.tools import TOOLS
from pydantic_ai.settings import ModelSettings
from agents.deps import FarmerContext
from pydantic import BaseModel, Field
from pydantic_ai import NativeOutput
import logging
logger = logging.getLogger(__name__)

# Gemma sometimes narrates a tool call ("I will check the details. One moment
# please.") and ends the turn without calling the tool, so the caller hears a
# promise that never resolves. Only short replies are checked: a real answer
# can legitimately contain words like "wait" ("wait two days after spraying").
_HOLD_MESSAGE_RE = re.compile(
    r"\b(one|a) moment\b|please wait|hold on|bear with me"
    r"|(i will|let me) (check|find|fetch|get|look|retrieve|provide|pull|share)"
    r"|एक (क्षण|पल)|कृपया प्रतीक्षा|प्रतीक्षा करें|रुकिए|इंतज़ार करें|इंतजार करें"
    r"|मैं .{0,20}(देखती|खोजती|निकालती|लाती) हूं",
    re.IGNORECASE,
)
_HOLD_MESSAGE_MAX_LEN = 200

class VoiceOutput(BaseModel):
    """Assistant's response to the user's query."""
    language: str = Field(
        default=DEFAULT_LANGUAGE,
        description=(
            "Internal code for the language already selected from X-Language. "
            "Do not detect or change the language."
        ),
    )
    lock_language: bool = Field(
        default=False,
        description=(
            "Always false. Sarvam supplies the detected language in X-Language."
        ),
    )
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

@voice_agent.output_validator
def reject_hold_messages(ctx: RunContext[FarmerContext], output: VoiceOutput) -> VoiceOutput:
    output.language = ctx.deps.language_code
    output.lock_language = False
    audio = (output.audio or "").strip()
    if len(audio) <= _HOLD_MESSAGE_MAX_LEN and _HOLD_MESSAGE_RE.search(audio):
        logger.warning(f"Rejected hold-message reply, retrying: {audio!r}")
        raise ModelRetry(
            "Your reply was only a promise to check and made the caller wait for an "
            "answer that never comes. Do not say you will check or ask the user to "
            "wait. Call the required tool now and answer with the actual information "
            "in this same response. Keep the conversation context: if a scheme or "
            "topic was already being discussed, answer for that same scheme or topic "
            "— do not ask the user to repeat it."
        )
    return output


@voice_agent.instructions
def get_voice_system_prompt(ctx: RunContext[FarmerContext]):
    language_code = ctx.deps.language_code
    prompt_file = f"voice_{language_code}"
    return get_prompt(
        prompt_file,
        context={
            'today_date': get_today_date_str(),
        },
    )
