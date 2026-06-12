from dataclasses import fields

from pydantic_ai import Agent, RunContext
from pydantic_ai.usage import UsageLimits
from helpers.utils import get_prompt, get_today_date_str, get_logger
from dotenv import load_dotenv
from agents.models import LLM_MODEL
from agents.tools import TOOLS
from pydantic_ai.settings import ModelSettings
from agents.deps import FarmerContext

logger = get_logger(__name__)

load_dotenv()

agrinet_vllm_settings = ModelSettings(
    temperature=0.7,
    top_p=0.9,
    presence_penalty=0.0,
    max_tokens=200,
    parallel_tool_calls=True,
    timeout=30,
    extra_body={
        "top_k": -1,
        "min_p": 0.0,
        "repetition_penalty": 1.0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
)

def _agrinet_vllm_usage_limits() -> UsageLimits:
    """Build limits compatible with pydantic-ai 0.2.x (no tool_calls_limit) and 1.x."""
    names = {f.name for f in fields(UsageLimits)}
    kw: dict = {"request_limit": 10, "total_tokens_limit": 100_000}
    if "tool_calls_limit" in names:
        kw["tool_calls_limit"] = 15
    return UsageLimits(**kw)


agrinet_vllm_usage_limits = _agrinet_vllm_usage_limits()

voice_agent = Agent(
    model=LLM_MODEL,
    name="Voice Agent",
    instrument=False,
    output_type=str,
    deps_type=FarmerContext,
    retries=1,
    tools=TOOLS,
    end_strategy="early",
    model_settings=agrinet_vllm_settings,
)

@voice_agent.system_prompt(dynamic=True)
def get_voice_system_prompt(ctx: RunContext[FarmerContext]) -> str:
    """Get the system prompt for the voice agent."""
    deps = ctx.deps
    target_lang = deps.target_lang if deps.target_lang else 'mr'
    logger.info(f"Target language: {target_lang}")
    return get_prompt(f'voice_system_{target_lang}', context={'today_date': get_today_date_str()})