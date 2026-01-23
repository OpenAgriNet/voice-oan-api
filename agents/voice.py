from typing import Any


import os
from pydantic_ai import Agent
from pydantic_ai.tools import RunContext
from datetime import datetime, timezone
from helpers.utils import get_prompt, get_today_date_str
from dotenv import load_dotenv
import logfire
from agents.models import LLM_MODEL
from agents.tools import TOOLS
from pydantic_ai.settings import ModelSettings
from pydantic import BaseModel, Field
from agents.deps import FarmerContext
from pydantic_ai import NativeOutput

load_dotenv()

logfire.configure(scrubbing=False, environment='bharatvistaar-voice')

class VoiceOutput(BaseModel):
    """Output of the voice agent."""
    audio: str = Field(description="The audio content of the response.")
    end_interaction: bool = Field(description="Set to true ONLY when the user explicitly indicates they have no more questions (e.g., responding 'no' to 'do you need any other info', saying 'no more questions', 'that's all', etc.). Default is false. Do not set to true for normal conversation flow or when asking follow-up questions.")

voice_agent = Agent[FarmerContext, NativeOutput(VoiceOutput)](
    model=LLM_MODEL,
    name="Voice Agent",
    instrument=True,
    output_type=NativeOutput(VoiceOutput),
    deps_type=FarmerContext,
    retries=3,
    tools=TOOLS,
    end_strategy='exhaustive',
    model_settings=ModelSettings(
         temperature=1.0,
         top_p=1.0,
        parallel_tool_calls=True,  
        openai_reasoning_effort='high',  
        openai_send_reasoning_ids=True,
    )
)

@voice_agent.system_prompt(dynamic=True)
def get_voice_system_prompt(ctx: RunContext[FarmerContext]):
    # Choose prompt based on target language (lang_code from deps)
    target_lang = ctx.deps.lang_code if ctx.deps.lang_code else 'hi'
    
    # Map language codes to prompt file names
    prompt_map = {
        'hi': 'voice_hi',
        'en': 'voice_en'
    }
    
    # Default to 'voice_hi' if language not in map
    prompt_name = prompt_map.get(target_lang, 'voice_hi')
    return get_prompt(prompt_name, context={'today_date': get_today_date_str()})