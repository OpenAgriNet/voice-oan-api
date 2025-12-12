import os
from pydantic_ai import Agent
from datetime import datetime, timezone
from helpers.utils import get_prompt, get_today_date_str
from dotenv import load_dotenv
import logfire
from agents.models import LLM_MODEL
from agents.tools import TOOLS
from pydantic_ai.settings import ModelSettings
from agents.deps import FarmerContext

load_dotenv()

logfire.configure(scrubbing=False)


voice_agent = Agent(
    model=LLM_MODEL,
    name="Voice Agent",
    instrument=True,
    output_type=str,
    deps=FarmerContext,
    retries=3,
    tools=TOOLS,
    #system_prompt=get_prompt('voice_system', context={'today_date': get_today_date_str()}),
    end_strategy='exhaustive',
    model_settings=ModelSettings(
        max_tokens=8192,
        parallel_tool_calls=True,
   )
)

@voice_agent.system_prompt(dynamic=True)
def get_voice_system_prompt():
    return get_prompt('voice_system_mr', context={'today_date': get_today_date_str()})