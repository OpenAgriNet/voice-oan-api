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
    # Gemma, factual voice agent (prices/weather, tool-grounded, 2-3 sentences).
    # Low temp for near-deterministic tool/grounding decisions; Gemma has no
    # Qwen3-style greedy degeneration, so the old 0.3 crutch is no longer needed.
    temperature=0.1,
    top_p=0.95,
    presence_penalty=0.0,
    parallel_tool_calls=True,
    timeout=30,
    extra_body={
        "top_k": 64,  # Gemma-native
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

def build_voice_system_prompt(deps: FarmerContext) -> str:
    """Render the voice agent's system prompt for the given deps (also used for Langfuse debug logging)."""
    target_lang = deps.target_lang if deps.target_lang else 'mr'
    base_prompt = get_prompt(f'voice_system_{target_lang}', context={'today_date': get_today_date_str()})
    if deps.user_memories:
        return f"{base_prompt}\n\n{deps.user_memories}"
    return base_prompt


@voice_agent.system_prompt(dynamic=True)
def get_voice_system_prompt(ctx: RunContext[FarmerContext]) -> str:
    """Get the system prompt for the voice agent."""
    logger.info(f"Target language: {ctx.deps.target_lang if ctx.deps.target_lang else 'mr'}")
    return build_voice_system_prompt(ctx.deps)