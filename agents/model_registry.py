from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from openai import APIConnectionError, AsyncAzureOpenAI
from httpx import TransportError
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "models.yaml"
_ENV_RE = re.compile(r"\$\{([^}]+)\}")
_ERROR_TYPES = {
    "ModelHTTPError": ModelHTTPError,
    "TimeoutError": TimeoutError,
    "APIConnectionError": APIConnectionError,
    "TransportError": TransportError,
    "UnexpectedModelBehavior": UnexpectedModelBehavior,
}


def _resolve(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_RE.sub(lambda match: os.getenv(match[1], ""), value)
    if isinstance(value, dict):
        return {key: _resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve(item) for item in value]
    return value


def _require(alias: str, config: dict, key: str) -> str:
    value = str(config.get(key) or "").strip()
    if not value:
        raise ValueError(f"Model alias '{alias}': {key} is required")
    return value


def _build_openai_compatible(alias: str, config: dict, api_key: str) -> Model:
    model_name = _require(alias, config, "model_name")
    provider_args = {"api_key": api_key}
    if base_url := str(config.get("base_url") or "").strip():
        provider_args["base_url"] = base_url.rstrip("/") + "/"
    api = config.get("api", "chat")
    if api == "chat":
        model_class = OpenAIChatModel
    elif api == "responses":
        model_class = OpenAIResponsesModel
    else:
        raise ValueError(f"Model alias '{alias}': unsupported API '{api}'")
    return model_class(model_name, provider=OpenAIProvider(**provider_args), settings=config.get("settings"))


def _build_openai(alias: str, config: dict) -> Model:
    return _build_openai_compatible(alias, config, _require(alias, config, "api_key"))


def _build_vllm(alias: str, config: dict) -> Model:
    return OpenAIChatModel(
        _require(alias, config, "model_name"),
        settings=config.get("settings"),
        provider=OpenAIProvider(
            base_url=_require(alias, config, "base_url"),
            api_key=str(config.get("api_key") or "not-required"),
        ),
    )


def _build_azure(alias: str, config: dict) -> Model:
    if config.get("base_url"):
        return _build_openai_compatible(alias, config, _require(alias, config, "api_key"))
    client = AsyncAzureOpenAI(
        azure_endpoint=_require(alias, config, "endpoint").rstrip("/"),
        api_key=_require(alias, config, "api_key"),
        api_version=_require(alias, config, "api_version"),
    )
    return OpenAIChatModel(
        _require(alias, config, "deployment_name"),
        settings=config.get("settings"),
        provider=OpenAIProvider(openai_client=client),
    )


_BUILDERS = {
    "azure-openai": _build_azure,
    "openai": _build_openai,
    "vllm": _build_vllm,
}


class ModelRegistry:
    def __init__(self, path: Path = _CONFIG_PATH):
        load_dotenv()
        config = _resolve(yaml.safe_load(path.read_text()) or {})
        self.models: dict[str, dict] = config.get("models", {})
        self.use_cases: dict[str, dict] = config.get("use_cases", {})
        self._cache: dict[str, Model] = {}

    def aliases(self, use_case: str) -> list[str]:
        return list(self.use_cases.get(use_case, {}).get("aliases", []))

    def proportions(self, use_case: str) -> list[int]:
        return [int(value) for value in self.use_cases.get(use_case, {}).get("proportions", [])]

    def default_alias(self, use_case: str) -> str:
        return str(self.use_cases.get(use_case, {}).get("default_alias", ""))

    def routing_ttl(self, use_case: str) -> int:
        return int(self.use_cases.get(use_case, {}).get("routing_ttl_seconds", 7200))

    def timeout(self, use_case: str) -> float:
        return float(self.use_cases.get(use_case, {}).get("timeout_seconds", 45))

    def fallback(self, alias: str) -> str | None:
        return self.models.get(alias, {}).get("fallback", {}).get("to")

    def fallback_errors(self, alias: str) -> tuple[type[BaseException], ...]:
        names = self.models.get(alias, {}).get("fallback", {}).get("on_error", [])
        return tuple(_ERROR_TYPES[name] for name in names)

    def concurrency_limit(self, alias: str) -> int | None:
        value = self.models.get(alias, {}).get("fallback", {}).get("on_concurrency_above")
        return int(value) if value is not None else None

    def metrics_ttl(self, alias: str) -> int:
        return int(self.models.get(alias, {}).get("fallback", {}).get("metrics_cache_ttl", 2))

    def metrics_url(self, alias: str) -> str | None:
        config = self.models.get(alias, {})
        override = str(config.get("fallback", {}).get("metrics_url") or "").strip()
        base_url = str(config.get("base_url") or "").strip()
        return override or (re.sub(r"/v1/?$", "", base_url) + "/metrics" if base_url else None)

    def model_name(self, alias: str) -> str:
        config = self.models[alias]
        return config.get("deployment_name") or config.get("model_name", alias)

    def get_model(self, alias: str) -> Model:
        if alias not in self._cache:
            config = self.models[alias]
            kind = config.get("kind", "")
            builder = _BUILDERS.get(kind)
            if not builder:
                raise ValueError(f"Model alias '{alias}': unsupported kind '{kind}'")
            self._cache[alias] = builder(alias, config)
        return self._cache[alias]

    def validate(self, use_case: str) -> None:
        aliases = self.aliases(use_case)
        weights = self.proportions(use_case)
        if not aliases or len(set(aliases)) != len(aliases):
            raise ValueError(f"Use case '{use_case}': aliases must be nonempty and unique")
        if len(weights) != len(aliases) or any(weight < 0 for weight in weights) or sum(weights) != 100:
            raise ValueError(f"Use case '{use_case}': proportions must match aliases and sum to 100")
        if self.default_alias(use_case) not in aliases:
            raise ValueError(f"Use case '{use_case}': default_alias must be one of its aliases")
        if self.routing_ttl(use_case) <= 0 or self.timeout(use_case) <= 0:
            raise ValueError(f"Use case '{use_case}': TTL and timeout must be positive")
        for alias in aliases:
            config = self.models.get(alias)
            if not config:
                raise ValueError(f"Use case '{use_case}' references unknown alias '{alias}'")
            fallback = self.fallback(alias)
            if fallback and (fallback not in self.models or fallback == alias):
                raise ValueError(f"Model alias '{alias}': invalid fallback")
            error_names = config.get("fallback", {}).get("on_error", [])
            if any(name not in _ERROR_TYPES for name in error_names):
                raise ValueError(f"Model alias '{alias}': unsupported fallback error")
            if self.concurrency_limit(alias) is not None and config.get("kind") != "vllm":
                raise ValueError(f"Model alias '{alias}': concurrency fallback requires vllm")
            if self.concurrency_limit(alias) is not None and not fallback:
                raise ValueError(f"Model alias '{alias}': concurrency fallback requires a target")
            self.get_model(alias)


@lru_cache(maxsize=1)
def get_registry() -> ModelRegistry:
    return ModelRegistry()
