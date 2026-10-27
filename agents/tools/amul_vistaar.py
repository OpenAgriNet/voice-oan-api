"""BH -> AmulVistaar cross-network scheme search."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry
from pydantic_ai.tools import RunContext

from agents.deps import FarmerContext
from app.config import get_default_httpx_timeout
from helpers.utils import get_logger

logger = get_logger(__name__)

DEFAULT_SCHEME_DOMAIN = "schemes:amul-union"
DEFAULT_SEARCH_TTL = "PT30S"


def _scheme_domain() -> str:
    return (os.getenv("AMUL_SCHEME_DOMAIN") or DEFAULT_SCHEME_DOMAIN).strip() or DEFAULT_SCHEME_DOMAIN


def _bap_search_url() -> str:
    endpoint = (os.getenv("BAP_ENDPOINT") or "").strip().rstrip("/")
    if not endpoint:
        raise ValueError("BAP_ENDPOINT is not configured")
    if endpoint.endswith("/search"):
        return endpoint
    return f"{endpoint}/search"


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _union_filter(union: Optional[str]) -> Optional[str]:
    normalized = (union or "").strip().lower()
    return normalized or None


def build_scheme_search_payload(
    query: str,
    union: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> Dict[str, Any]:
    normalized_query = (query or "").strip()
    normalized_union = _union_filter(union)
    normalized_provider_id = (provider_id or "").strip() or None

    intent: Dict[str, Any] = {}
    if normalized_provider_id:
        intent["provider"] = {"id": normalized_provider_id}
    if normalized_query:
        intent["item"] = {"descriptor": {"name": normalized_query}}
    if normalized_union:
        intent["tags"] = [
            {
                "descriptor": {"code": "filter"},
                "list": [
                    {
                        "descriptor": {"code": "union"},
                        "value": normalized_union,
                    }
                ],
            }
        ]

    return {
        "context": {
            "domain": _scheme_domain(),
            "action": "search",
            "version": "1.1.0",
            "bap_id": os.getenv("BAP_ID"),
            "bap_uri": os.getenv("BAP_URI"),
            "transaction_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
            "timestamp": _iso_timestamp(),
            "ttl": os.getenv("AMUL_SEARCH_TTL", DEFAULT_SEARCH_TTL),
        },
        "message": {
            "intent": intent,
        },
    }


class AmulCrossNetworkSchemeResponse(BaseModel):
    responses: list = Field(default_factory=list)

    def _has_data(self) -> bool:
        for block in self.responses:
            if not isinstance(block, dict):
                continue
            message = block.get("message") or {}
            catalog = message.get("catalog") or {}
            for provider in catalog.get("providers") or []:
                if provider.get("items"):
                    return True
        return False

    @staticmethod
    def _append_attribute_lines(parts: List[str], item: Dict[str, Any]) -> None:
        for tag in item.get("tags") or []:
            descriptor = tag.get("descriptor") or {}
            if descriptor.get("code") != "attributes":
                continue
            for entry in tag.get("list") or []:
                entry_descriptor = entry.get("descriptor") or {}
                code = entry_descriptor.get("name") or entry_descriptor.get("code")
                value = (entry.get("value") or "").strip()
                if code and value and value.lower() != "null":
                    parts.append(f"- {code.replace('_', ' ').title()}: {value}")

    def __str__(self) -> str:
        if not self.responses or not self._has_data():
            return "No scheme data found for this scheme."

        parts: List[str] = ["**Source:** Government Scheme Information", ""]

        for block in self.responses:
            if not isinstance(block, dict):
                continue
            message = block.get("message") or {}
            catalog = message.get("catalog") or {}
            for provider in catalog.get("providers") or []:
                provider_name = ((provider.get("descriptor") or {}).get("name") or provider.get("id") or "").strip()
                for item in provider.get("items") or []:
                    descriptor = item.get("descriptor") or {}
                    title = (descriptor.get("name") or item.get("id") or "Scheme").strip()
                    short_desc = (descriptor.get("short_desc") or "").strip()
                    long_desc = (descriptor.get("long_desc") or "").strip()

                    parts.append(f"## {title}")
                    if provider_name:
                        parts.append(f"Provider: {provider_name}")
                    if short_desc:
                        parts.append(short_desc)
                    if long_desc and long_desc != short_desc:
                        parts.append(long_desc)
                    self._append_attribute_lines(parts, item)
                    parts.append("")

        if len(parts) <= 2:
            return json.dumps(self.responses, ensure_ascii=False, indent=2)
        return "\n".join(parts).strip()


async def call_amul_vistaar_network(
    ctx: RunContext[FarmerContext],
    query: str,
    union: Optional[str] = None,
    provider_id: Optional[str] = None,
) -> str:
    """Fetch Amul union scheme info via network search.

    Use for Amul union scheme queries available over the cross-network catalog.
    Pass the farmer's scheme query in `query`. Optionally narrow the search with
    a supported union filter (`banas`, `kutch`, `sumul`, `surendranagar`) or a
    canonical provider_id (for example `banas-union`).

    Args:
        query: Free-text scheme query. Use a specific scheme phrase when possible.
        union: Optional union filter such as "banas" or "sumul".
        provider_id: Optional canonical provider ID such as "banas-union".
    """
    query = (query or "").strip()
    union = _union_filter(union)
    provider_id = (provider_id or "").strip() or None
    if not query and not union and not provider_id:
        raise ModelRetry(
            "Provide at least one of query, union, or provider_id for Amul scheme search. "
            "Example: query='cattle insurance', union='banas'."
        )

    payload = build_scheme_search_payload(query=query, union=union, provider_id=provider_id)
    payload["context"]["tags"] = {
        "session_id": ctx.deps.session_id or "",
        "question_id": ctx.deps.question_id or "",
    }


    try:
        url = _bap_search_url()
        logger.info(
            "Beckn [call_amul_vistaar_network/search] URL: %s domain=%s",
            url,
            payload["context"]["domain"],
        )
        logger.info(
            "Beckn [call_amul_vistaar_network/search] payload: %s",
            json.dumps(payload, ensure_ascii=False),
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(url, json=payload, timeout=get_default_httpx_timeout())

        logger.info("Beckn [call_amul_vistaar_network/search] status: %s", response.status_code)

        if response.status_code != 200:
            logger.error(
                "AmulVistaar network search returned %s: %s",
                response.status_code,
                (response.text or "")[:500],
            )
            return "Scheme information is temporarily unavailable. Please try again later."

        try:
            data = response.json()
        except json.JSONDecodeError:
            return (response.text or "").strip() or "AmulVistaar network returned a non-JSON response."

        responses = data.get("responses", data if isinstance(data, list) else [])
        if not isinstance(responses, list):
            responses = []

        result = str(AmulCrossNetworkSchemeResponse.model_validate({"responses": responses}))
        return result

    except ValueError as e:
        logger.error("AmulVistaar cross-network config error: %s", e)
        return "AmulVistaar cross-network is not configured. Set BAP_ENDPOINT, BAP_ID, and BAP_URI."
    except httpx.TimeoutException:
        logger.error("AmulVistaar network request timed out")
        return "AmulVistaar request timed out. Please try again later."
    except httpx.RequestError as e:
        logger.error("AmulVistaar network request failed: %s", e)
        return f"AmulVistaar request failed: {e!s}"
    except Exception as e:
        logger.error("Unexpected AmulVistaar network error: %s", e)
        raise ModelRetry(f"Unexpected error calling AmulVistaar network. {e!s}") from e