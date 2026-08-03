from pydantic_ai import Agent, RunContext
from helpers.utils import get_prompt, get_today_date_str, get_logger
from dotenv import load_dotenv
from agents.models import LLM_MODEL
from agents.tools import TOOLS
from pydantic_ai.settings import ModelSettings
from agents.deps import FarmerContext
from app.langfuse_pydantic_ai import langfuse_event_stream_handler

logger = get_logger(__name__)

load_dotenv()

voice_agent = Agent(
    model=LLM_MODEL,
    name="Voice Agent",
    instrument=False,
    output_type=str,
    deps=FarmerContext,
    retries=3,
    tools=TOOLS,
    #system_prompt=get_prompt('voice_system', context={'today_date': get_today_date_str()}),
    end_strategy='exhaustive',
    event_stream_handler=langfuse_event_stream_handler,
    model_settings=ModelSettings(
        max_tokens=8192,
        parallel_tool_calls=True,
   )
)

@voice_agent.system_prompt(dynamic=True)
def get_voice_system_prompt(ctx: RunContext[FarmerContext]) -> str:
    """Get the system prompt for the voice agent."""
    deps = ctx.deps
    prompt_lang = deps.prompt_lang or deps.target_lang or 'mr'
    logger.info(f"Prompt language: {prompt_lang}")
    return get_prompt(f'voice_system_{prompt_lang}', context={'today_date': get_today_date_str()})