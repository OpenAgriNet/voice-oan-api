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
        # Low temperature + fixed seed: factual agri answers must be stable across
        temperature=0.2,
        seed=42,
   )
)

@voice_agent.system_prompt(dynamic=True)
def get_voice_system_prompt(ctx: RunContext[FarmerContext]) -> str:
    """Get the system prompt for the voice agent."""
    deps = ctx.deps
    target_lang = deps.target_lang if deps.target_lang else 'mr'
    logger.info(f"Target language: {target_lang}")
    return get_prompt(f'voice_system_{target_lang}', context={'today_date': get_today_date_str()})