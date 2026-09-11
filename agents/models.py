"""Voice models loaded from the shared alias registry."""
from agents.model_registry import get_registry

VOICE_USE_CASE = "voice"
_registry = get_registry()
_registry.validate(VOICE_USE_CASE)

VOICE_DEFAULT_ALIAS = _registry.default_alias(VOICE_USE_CASE)
LLM_AGRINET_MODEL = _registry.get_model(VOICE_DEFAULT_ALIAS)
LLM_MODEL = LLM_AGRINET_MODEL


def get_voice_model(alias: str):
    return _registry.get_model(alias)
