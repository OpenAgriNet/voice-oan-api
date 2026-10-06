"""SMAM (Sub-Mission on Agricultural Mechanization) beneficiary application status via BAP /search."""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

import httpx
from pydantic import AnyHttpUrl, BaseModel, Field
from pydantic_ai import ModelRetry, UnexpectedModelBehavior

from app.config import get_default_httpx_timeout
from agents.deps import FarmerContext
from helpers.utils import get_logger, to_ascii_digits
from pydantic_ai.tools import RunContext

logger = get_logger(__name__)

SmamSearchType = Literal["application_no", "mobile"]


class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None


class TagListEntry(BaseModel):
    descriptor: Descriptor
    value: str


class CatalogTagGroup(BaseModel):
    descriptor: Descriptor
    list: List[TagListEntry]


class ItemTagGroup(BaseModel):
    descriptor: Descriptor
    list: List[TagListEntry]
    display: Optional[bool] = None


class SmamItem(BaseModel):
    id: str
    descriptor: Descriptor
    tags: List[ItemTagGroup] = Field(default_factory=list)


class SmamProvider(BaseModel):
    id: str
    descriptor: Descriptor
    items: List[SmamItem] = Field(default_factory=list)


class SmamCatalog(BaseModel):
    descriptor: Descriptor
    tags: Optional[List[CatalogTagGroup]] = None
    providers: List[SmamProvider] = Field(default_factory=list)


class SmamMessage(BaseModel):
    catalog: SmamCatalog


class Country(BaseModel):
    code: str


class City(BaseModel):
    code: str


class BecknLocation(BaseModel):
    country: Country
    city: City


class Context(BaseModel):
    ttl: Optional[str] = None
    action: str
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    domain: Optional[str] = None
    version: Optional[str] = None
    bap_id: Optional[str] = None
    bap_uri: Optional[AnyHttpUrl] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[AnyHttpUrl] = None
    location: Optional[BecknLocation] = None


class SmamResponseItem(BaseModel):
    context: Context
    message: SmamMessage


def _flatten_catalog_tag_values(catalog: SmamCatalog) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    if not catalog.tags:
        return merged
    for grp in catalog.tags:
        for ent in grp.list:
            key = (ent.descriptor.code or "").strip().lower().replace("-", "_")
            if key:
                merged[key] = ent.value
    return merged


class SmamStatusApiResponse(BaseModel):
    context: Optional[Context] = None
    responses: List[SmamResponseItem] = Field(default_factory=list)

    def __str__(self) -> str:
        lines: List[str] = []
        for rsp in self.responses:
            cat = rsp.message.catalog
            ctx_map = _flatten_catalog_tag_values(cat)

            status = ctx_map.get("status", "")
            message = ctx_map.get("message", "")
            if status and status.lower() != "success":
                if message:
                    return f"Status: {status}. {message}"
                return f"Status: {status}. No application details were returned."

            if not cat.providers:
                return message or "No application data found for the details provided."

            for prov in cat.providers:
                app_label = prov.descriptor.name or prov.id
                lines.append(f"## Application / reference: {app_label}")
                for item in prov.items:
                    impl_name = item.descriptor.name or item.id
                    lines.append(f"### Implement: {impl_name}")
                    for tgrp in item.tags:
                        tcode = (tgrp.descriptor.code or "").lower().replace("-", "_")
                        if tcode == "implement_status":
                            for ent in tgrp.list:
                                label = ent.descriptor.name or ent.descriptor.code or "Detail"
                                lines.append(f"- **{label}:** {ent.value}")
                        elif tcode == "status_history":
                            lines.append("**Status history (latest first):**")
                            for ent in tgrp.list:
                                raw = ent.value.strip()
                                try:
                                    if raw.startswith("{"):
                                        hist = json.loads(raw)
                                        st = hist.get("status_text") or str(
                                            hist.get("status_code", "")
                                        )
                                        sd = (hist.get("status_date") or "").strip()
                                        if sd:
                                            lines.append(f"- Status on ({sd}): {st}")
                                        else:
                                            lines.append(f"- Status: {st}")
                                    else:
                                        lines.append(f"- {raw}")
                                except json.JSONDecodeError:
                                    lines.append(f"- {raw}")
                    lines.append("")

        out = "\n".join(lines).strip()
        return out if out else "No application data found for the details provided."


class SmamStatusRequest(BaseModel):
    search_type: SmamSearchType
    search_value: str
    session_id: str = ""
    question_id: str = ""

    def get_payload(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        return {
            "context": {
                "domain": "schemes:vistaar",
                "action": "search",
                "version": "1.1.0",
                "bap_id": os.getenv("BAP_ID"),
                "bap_uri": os.getenv("BAP_URI"),
                "bpp_id": os.getenv("BPP_ID"),
                "bpp_uri": os.getenv("BPP_URI"),
                "message_id": str(uuid.uuid4()),
                "transaction_id": str(uuid.uuid4()),
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ttl": "PT10M",
                "location": {
                    "country": {"code": "IND"},
                    "city": {"code": "*"},
                },
                "tags": {
                    "session_id": self.session_id,
                    "question_id": self.question_id,
                },
            },
            "message": {
                "intent": {
                    "provider": {
                        "id": "smam",
                        "descriptor": {"code": "smam"},
                    },
                    "item": {
                        "descriptor": {"code": "application_status"},
                        "tags": [
                            {
                                "descriptor": {"code": "search_params"},
                                "list": [
                                    {"descriptor": {"code": "search_type"}, "value": self.search_type},
                                    {"descriptor": {"code": "search_value"}, "value": self.search_value},
                                ],
                            }
                        ],
                    },
                }
            },
        }


def _strip_leading_country_or_zero(digits: str) -> str:
    """Normalize Indian mobile digits: strip leading `91` or `0` when applicable."""
    n = len(digits)
    if n == 12 and digits[:2] == "91" and digits[2] in "6789":
        # Only strip when it looks like an Indian mobile (prevents mangling 12-digit IDs).
        return digits[2:]
    if n == 11 and digits[:1] == "0":
        return digits[1:]
    return digits


def _normalize_search_value(search_type: SmamSearchType, raw: str) -> str:
    v = to_ascii_digits(raw).strip()
    if not v:
        raise ValueError("Search value cannot be empty.")

    def _digits_only(s: str) -> str:
        return "".join(c for c in s if c.isdigit())

    if search_type == "mobile":
        digits = _strip_leading_country_or_zero(_digits_only(v))
        if len(digits) != 10:
            raise ValueError(
                "Invalid mobile number. Please provide a 10-digit Indian mobile number "
                "(you may include country code 91 or leading 0)."
            )
        return digits

    # application_no: preserve alphanumeric refs; for numeric-only inputs, normalize digits.
    if search_type == "application_no" and not re.search(r"[A-Za-z]", v):
        digits = _digits_only(v)
        return _strip_leading_country_or_zero(digits) if digits else v

    return v


async def check_smam_scheme_status(
    ctx: RunContext[FarmerContext],
    search_type: SmamSearchType,
    search_value: str,
) -> str:
    """Check SMAM (Sub-Mission on Agricultural Mechanization) application / beneficiary status.

    Use this tool when the farmer wants to check SMAM subsidy or application status.

    Args:
        search_type: Lookup type — `mobile` (10-digit Indian) or `application_no` (application reference
            or numeric id).
        search_value: Identifier shared by the farmer (no placeholders). Numeric values are
            digit-normalized when applicable (e.g. `+91` / leading `0` removed for mobile-shaped inputs).

    Returns:
        Human-readable status with implement details and status history (when available).
    """
    try:
        normalized = _normalize_search_value(search_type, search_value)
    except ValueError as e:
        return str(e)


    try:
        payload = SmamStatusRequest(
            search_type=search_type,
            search_value=normalized,
            session_id=ctx.deps.session_id,
            question_id=ctx.deps.question_id,
        ).get_payload()
        endpoint = os.getenv("BAP_ENDPOINT")
        if not endpoint:
            logger.error("BAP_ENDPOINT is not set")
            return "Scheme status service is not configured. Please try again later."

        logger.info("SMAM status request: search_type=%s", search_type)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                endpoint.rstrip("/") + "/search",
                json=payload,
                timeout=get_default_httpx_timeout(),
            )

            if response.status_code != 200:
                logger.error("SMAM status API returned status code %s", response.status_code)
                return "SMAM application status service is unavailable. Please try again later."

            data = response.json()
            logger.debug(
                "SMAM status response keys: %s",
                list(data.keys()) if isinstance(data, dict) else type(data),
            )

            parsed = SmamStatusApiResponse.model_validate(data)
            out = str(parsed)
            return out

    except httpx.TimeoutException as e:
        logger.error("SMAM status API timed out: %s", e)
        return "SMAM status request timed out. Please try again later."

    except httpx.RequestError as e:
        logger.error("SMAM status API request failed: %s", e)
        return f"SMAM status request failed: {e!s}"

    except UnexpectedModelBehavior:
        logger.warning("SMAM status exceeded retry limit")
        return "SMAM status is temporarily unavailable. Please try again later."

    except Exception as e:
        logger.error("Error in SMAM status check: %s", e)
        raise ModelRetry(f"Unexpected error in SMAM status request. {e!s}") from e
