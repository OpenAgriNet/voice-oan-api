"""Capability loading preserves original business tools and their prepare hooks."""
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader
from pydantic_ai import ModelRetry, RunContext, Tool

from agents.capability_manifest import CAPABILITIES
from agents.capability_policy import ALL_TOOLS

PROMPT_DIR = Path(__file__).resolve().parents[1] / "assets" / "prompts"


@lru_cache(maxsize=1)
def _prompt_assets():
    return (
        Environment(loader=FileSystemLoader(PROMPT_DIR), autoescape=False),
        json.loads((PROMPT_DIR / "capabilities" / "sections.json").read_text()),
    )


def validate_manifest(tools: list) -> None:
    names = [tool.name for tool in tools]
    grouped = [name for group in CAPABILITIES.values() for name in group]
    if set(names) != ALL_TOOLS or len(names) != len(set(names)) or len(grouped) != len(set(grouped)):
        raise ValueError("Capability manifest must cover every business tool exactly once")


async def load_capability(ctx: RunContext[Any], capability: str) -> dict:
    """Load a capability's permitted tools and workflow instructions; execute no business action."""
    state = ctx.deps.capabilities
    if capability not in CAPABILITIES:
        raise ModelRetry("Choose a capability from the available capability manifest.")
    permitted = CAPABILITIES[capability] & state.policy.allowed_tools
    if not permitted:
        raise ModelRetry("This capability is not permitted for this session.")
    state.loaded_tools.update(permitted)
    return {
        "capability": capability,
        "loaded_tools": sorted(permitted),
        "instructions": "\n".join(text for names, text in state.sections if names & permitted),
        "note": "Loading is not execution. Now follow the workflow using the supplied conversation details.",
    }


CAPABILITY_LOADER = Tool(load_capability, takes_ctx=True, sequential=True, strict=False)


async def prepare_capability_tools(ctx: RunContext[Any], tools: list) -> list:
    state = ctx.deps.capabilities
    visible = state.visible
    pending = state.policy.allowed_tools - visible
    # Any existing tool-specific prepare hooks have already run.
    return [tool for tool in tools if tool.name in visible or (tool.name == "load_capability" and pending)]


def capability_prompt_context(deps: Any, prompt_file: str, context: dict) -> dict:
    state = deps.capabilities
    if state.policy.allowed_tools - state.visible:
        env, manifest = _prompt_assets()
        state.sections = [
            (frozenset(section["tools"]), env.get_template(section["path"]).render(**context))
            for section in manifest.get(prompt_file, [])
        ]
    return {**context, "capability_enabled": lambda names: bool(set(names) & state.visible)}


def capability_routing(deps: Any) -> str:
    state = deps.capabilities
    if state.visible == ALL_TOOLS:
        return ""
    available = {
        group: sorted(names & state.policy.allowed_tools)
        for group, names in CAPABILITIES.items() if names & state.policy.allowed_tools
    }
    pending = state.policy.allowed_tools - state.visible
    return (
        "\n\n## Session tool availability (overrides general service descriptions)\n"
        "Only the following permitted tools may be used or offered in this session. "
        "Other tools mentioned in shared workflow rules are unavailable; do not invent their results.\n"
        + json.dumps(available, ensure_ascii=False)
        + "\nLoaded now: " + ", ".join(sorted(state.visible))
        + ("\nFor an unloaded workflow, call load_capability with its group name before collecting "
           "workflow details or answering. Its result supplies the original workflow instructions and "
           "makes permitted tools available on the next model step. Loading is not execution. "
           "Reuse conversation details; do not narrate loading. Loaded tools remain available for this session."
           if pending else "")
    )
