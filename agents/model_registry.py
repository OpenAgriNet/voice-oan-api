from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from openai import AsyncAzureOpenAI
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIResponsesModel
from pydantic_ai.providers.openai import OpenAIProvider

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "models.yaml"
_ENV_RE = re.compile(r"\$\{([^}]+)\}")
_ERROR_TYPES = {"ModelHTTPError": ModelHTTPError, "TimeoutError": TimeoutError}


def _resolve(value):
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


class ModelRegistry:
    def __init__(self, path: Path = _CONFIG_PATH):
        load_dotenv()
        config = _resolve(yaml.safe_load(path.read_text()) or {})
        self.models = config.get("models", {})
        self.use_cases = config.get("use_cases", {})
        self._cache = {}

    def aliases(self, use_case: str) -> list[str]:
        return list(self.use_cases.get(use_case, {}).get("aliases", []))

    def proportions(self, use_case: str) -> list[int]:
        values = self.use_cases.get(use_case, {}).get("proportions", [])
        return [int(value) for value in values]

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
        return config["deployment_name"] if config["kind"] == "azure-openai" else config["model_name"]

    def get_model(self, alias: str):
        if alias in self._cache:
            return self._cache[alias]
        config = self.models[alias]
        kind = config["kind"]
        if kind == "azure-openai":
            client = AsyncAzureOpenAI(
                azure_endpoint=_require(alias, config, "endpoint").rstrip("/"),
                api_key=_require(alias, config, "api_key"),
                api_version=_require(alias, config, "api_version"),
            )
            provider = OpenAIProvider(openai_client=client)
            model_class = OpenAIChatModel
        else:
            provider_args = {"api_key": config.get("api_key") or "not-required"}
            if config.get("base_url"):
                provider_args["base_url"] = str(config["base_url"]).rstrip("/") + "/"
            provider = OpenAIProvider(**provider_args)
            model_class = OpenAIResponsesModel if config.get("api") == "responses" else OpenAIChatModel
        self._cache[alias] = model_class(self.model_name(alias), provider=provider)
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
            kind = config.get("kind")
            if kind not in {"openai", "vllm", "azure-openai", "bharat_ai_grid"}:
                raise ValueError(f"Model alias '{alias}': unsupported kind '{kind}'")
            if kind == "azure-openai":
                for key in ("endpoint", "api_key", "api_version", "deployment_name"):
                    _require(alias, config, key)
            else:
                _require(alias, config, "model_name")
                if kind in {"vllm", "bharat_ai_grid"}:
                    _require(alias, config, "base_url")
                if kind == "openai":
                    _require(alias, config, "api_key")
            fallback = self.fallback(alias)
            if fallback and (fallback not in self.models or fallback == alias):
                raise ValueError(f"Model alias '{alias}': invalid fallback")
            error_names = config.get("fallback", {}).get("on_error", [])
            if any(name not in _ERROR_TYPES for name in error_names):
                raise ValueError(f"Model alias '{alias}': unsupported fallback error")
            if self.concurrency_limit(alias) is not None and kind != "vllm":
                raise ValueError(f"Model alias '{alias}': concurrency fallback requires vllm")
            if self.concurrency_limit(alias) is not None and not fallback:
                raise ValueError(f"Model alias '{alias}': concurrency fallback requires a target")
            self.get_model(alias)


@lru_cache(maxsize=1)
def get_registry() -> ModelRegistry:
    return ModelRegistry()
