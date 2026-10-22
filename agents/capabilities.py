"""Capability loading preserves original business tools and their prepare hooks."""
import json
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from helpers.prompt_skills import prompt_environment, section_index
from pydantic_ai import RunContext
from pydantic_ai.capabilities import Capability, CombinedCapability
from helpers.prompt_skills import skill_catalog

from agents.capability_manifest import CAPABILITIES
from agents.capability_policy import ALL_TOOLS

PROMPT_DIR = Path(__file__).resolve().parents[1] / "assets" / "prompts"


@lru_cache(maxsize=1)
def _prompt_assets():
    return (
        prompt_environment(PROMPT_DIR),
        section_index(PROMPT_DIR),
    )


def validate_manifest(tools: list) -> None:
    names = [tool.name for tool in tools]
    grouped = [name for group in CAPABILITIES.values() for name in group]
    missing = sorted(ALL_TOOLS - set(names))
    extra = sorted(set(names) - ALL_TOOLS)
    duplicate_tools = sorted(name for name in set(names) if names.count(name) > 1)
    if missing or extra or duplicate_tools:
        print(
            "[capabilities.validate_manifest] failed "
            f"missing_in_tools={missing}; extra_in_tools={extra}; "
            f"duplicate_tools={duplicate_tools}; "
            f"tools_count={len(names)}; manifest_count={len(grouped)}",
            file=sys.stderr,
            flush=True,
        )
        raise ValueError(
            "Skills must cover every business tool; register each implementation exactly once. "
            f"missing_in_tools={missing}; extra_in_tools={extra}; "
            f"duplicate_tools={duplicate_tools}"
        )


def skill_capabilities(ctx: RunContext[Any]) -> CombinedCapability:
    """Build session-local native capabilities, without duplicating shared tools.

    Preloaded instructions stay in their original prompt positions. Deferred ones
    arrive through Pydantic's loader. The shared registry retains prepare guards.
    """
    state = ctx.deps.capabilities
    state.restore(ctx.messages)
    state.prompt_tools = state.visible
    state.sections = []
    state.delivered_sections.clear()

    def instructions_for(permitted):
        def instructions(load_ctx: RunContext[Any]) -> str:
            current = load_ctx.deps.capabilities
            selected = [(index, text) for index, (names, text) in enumerate(current.sections)
                        if names & permitted and index not in current.delivered_sections]
            current.delivered_sections.update(index for index, _ in selected)
            return "\n".join(text for _, text in selected)
        return instructions

    packages = skill_catalog()
    return CombinedCapability([
        Capability(
            id=name, description=packages[name]["description"],
            defer_loading=not permitted <= state.visible,
            instructions=instructions_for(permitted) if not permitted <= state.visible else None,
        )
        for name, tools in CAPABILITIES.items()
        if (permitted := tools & state.policy.allowed_tools)
    ])


async def prepare_capability_tools(ctx: RunContext[Any], tools: list) -> list:
    state = ctx.deps.capabilities
    # V2 prepares tools before evaluating dynamic instructions.
    state.restore(getattr(ctx, "messages", ()))
    for name in getattr(ctx, "active_capability_ids", ()):
        state.loaded_tools.update(CAPABILITIES.get(name, set()) & state.policy.allowed_tools)
    visible = state.visible
    pending = state.policy.allowed_tools - visible
    # Tool-specific prepare hooks (e.g. AgriStack consent) have already run.
    return [tool for tool in tools if tool.name in visible or (tool.name == "load_capability" and pending)]


def capability_prompt_context(deps: Any, prompt_file: str, context: dict) -> dict:
    state = deps.capabilities
    visible = state.visible if state.prompt_tools is None else state.prompt_tools
    if state.policy.allowed_tools - visible:
        env, manifest = _prompt_assets()
        state.sections = [
            (frozenset(section["tools"]), env.get_template(section["path"]).render(**context))
            for section in manifest.get(prompt_file, [])
        ]
        state.delivered_sections.update(index for index, (names, _) in enumerate(state.sections)
                                        if names & visible)
    return {**context, "capability_enabled": lambda names: bool(set(names) & visible)}


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
        + ("\nFor an unloaded workflow, call load_capability with its id before collecting "
           "workflow details or answering. Its result supplies the original workflow instructions and "
           "makes permitted tools available on the next model step. Loading is not execution. "
           "Reuse conversation details; do not narrate loading. Loaded tools remain available for this session."
           if pending else "")
    )
