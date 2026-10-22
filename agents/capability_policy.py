"""Session-local tool policy. No HTTP/API parameters are wired to this yet."""
from typing import Self

from decouple import config
from pydantic import BaseModel, Field, model_validator

from agents.capability_manifest import CAPABILITIES

ALL_TOOLS = frozenset(tool for tools in CAPABILITIES.values() for tool in tools)


class ToolPolicy(BaseModel):
    allowed_tools: frozenset[str] = ALL_TOOLS
    # None means all allowed tools, not all tools in the repository.
    preloaded_tools: frozenset[str] | None = None

    @model_validator(mode="after")
    def validate_tools(self) -> Self:
        unknown = self.allowed_tools - ALL_TOOLS
        if unknown:
            raise ValueError(f"Unknown allowed tools: {sorted(unknown)}")
        if self.preloaded_tools is not None and not self.preloaded_tools <= self.allowed_tools:
            raise ValueError("Preloaded tools must be a subset of allowed tools")
        return self

    model_config = {"frozen": True}


def configured_tool_policy() -> ToolPolicy:
    def names(value: str, all_names: frozenset[str]) -> frozenset[str]:
        if value.strip().lower() == "all":
            return all_names
        if value.strip().lower() == "none":
            return frozenset()
        return frozenset(name.strip() for name in value.split(",") if name.strip())

    allowed = names(config("CAPABILITY_ALLOWED_TOOLS", default="all"), ALL_TOOLS)
    preloaded = names(config("CAPABILITY_PRELOADED_TOOLS", default="all"), allowed)
    return ToolPolicy(allowed_tools=allowed, preloaded_tools=preloaded)


def _loaded_tools_from_return(part: object) -> set[str]:
    """Ignore unrelated or malformed history; accept only tools in the named group."""
    if getattr(part, "part_kind", None) != "tool-return" or getattr(part, "tool_name", None) != "load_capability":
        return set()
    content = getattr(part, "content", None)
    if not isinstance(content, dict):
        return set()
    capability = content.get("capability")
    if not isinstance(capability, str) or capability not in CAPABILITIES:
        return set()
    loaded = content.get("loaded_tools", ())
    if not isinstance(loaded, (list, tuple)) or not all(isinstance(name, str) for name in loaded):
        return set()
    return set(loaded) & CAPABILITIES[capability]


class CapabilityState(BaseModel):
    policy: ToolPolicy = Field(default_factory=configured_tool_policy)
    loaded_tools: set[str] = Field(default_factory=set)
    # Rendered workflow instructions for loader results; not persisted as session state.
    sections: list[tuple[frozenset[str], str]] = Field(default_factory=list, exclude=True)
    prompt_tools: frozenset[str] | None = Field(default=None, exclude=True)
    delivered_sections: set[int] = Field(default_factory=set, exclude=True)

    @property
    def visible(self) -> frozenset[str]:
        initial = self.policy.preloaded_tools
        if initial is None:
            initial = self.policy.allowed_tools
        return (initial | self.loaded_tools) & self.policy.allowed_tools

    def restore(self, history: list) -> None:
        """Restore only successful loader results, before callers trim history."""
        native_calls = {}
        for message in history:
            for part in getattr(message, "parts", ()):
                if getattr(part, "tool_kind", None) == "capability-load":
                    if getattr(part, "part_kind", None) == "tool-call":
                        name = getattr(part, "capability_id", None)
                        if name in CAPABILITIES:
                            native_calls[part.tool_call_id] = name
                    elif getattr(part, "part_kind", None) == "tool-return":
                        name = native_calls.get(part.tool_call_id)
                        if name is not None:
                            self.loaded_tools.update(CAPABILITIES[name] & self.policy.allowed_tools)
                # Never trust persisted names to bypass the current policy.
                # Retain compatibility with sessions created by the previous loader.
                self.loaded_tools.update(_loaded_tools_from_return(part) & self.policy.allowed_tools)
