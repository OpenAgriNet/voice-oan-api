"""
Internal backends for farmer and animal data from multiple APIs.
- amulpashudhan.com (PASHUGPT_TOKEN): GetFarmerDetailsByMobile, GetAnimalDetailsByTagNo,
  GetAITechniciansBySociety, CreateAICall, CreateHealthCall
- herdman.live (PASHUGPT_TOKEN_3): get-amul-farmer, get-amul-animal

Used by farmer.py and animal.py to provide cohesive tools with fallback and merged output.
"""
import json
import re
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, List, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from agents.models.farmer import FarmerRecord, AnimalRecord
from agents.models.ai_call import AICallRequestModel, AICallResponseModel
from agents.models.health_call import HealthCallRequestModel, HealthCallResponseModel
from app.models.milk_collection import (
    FarmerMilkCollectionRequestModel,
    FarmerMilkCollectionResponseModel,
)
from app.config import settings
from app.observability import start_observation
from helpers.utils import get_logger

_logger = get_logger(__name__)

BASE_AMULPASHUDHAN = "https://api.amulpashudhan.com/configman/v1/PashuGPT"
BASE_HERDMAN = "https://herdman.live/apis/api"


class BackendUnavailableError(RuntimeError):
    """Upstream did not answer, or answered with an error status.

    Distinct from "upstream answered, and this farmer/animal has no record".
    Collapsing the two is what let a transient 500 be cached as a 2h `not_found`
    for a cold caller, which is how a farmer ends up being told their details are
    not available and the agent ends up inventing identifiers.
    """

    def __init__(self, provider: str, detail: str) -> None:
        super().__init__(f"{provider}: {detail}")
        self.provider = provider
        self.detail = detail


# Why this read happened — tags every API observation so we can tell, in
# Langfuse, a request-time cold fetch from a background-worker refresh. Reads
# served straight from Redis never reach this layer, so a recorded API call
# always means the cache was bypassed.
_fetch_reason: ContextVar[str] = ContextVar("farmer_fetch_reason", default="request")


@contextmanager
def fetch_reason(reason: str):
    """Tag all Amul API calls made within this block with `reason`."""
    token = _fetch_reason.set(reason)
    try:
        yield
    finally:
        _fetch_reason.reset(token)


def current_fetch_reason() -> str:
    return _fetch_reason.get()


def _safe_response_summary(body: str) -> dict:
    """PII-safe shape of a response: record count + which keys are present/null,
    WITHOUT any values. This is what proves an inconsistent return (e.g. a record
    that came back missing `totalAnimals`) without shipping farmer PII to Langfuse.
    """
    out: dict[str, Any] = {"bytes": len(body)}
    if not body.strip():
        out["json"] = False
        return out
    try:
        data = json.loads(body)
    except Exception:
        out["json"] = False
        return out
    out["json"] = True
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if isinstance(data, list):
        out["records"] = len(data)
        first = data[0] if data and isinstance(data[0], dict) else None
    elif isinstance(data, dict):
        out["records"] = 1
        first = data
    else:
        out["records"] = 0
        first = None
    if isinstance(first, dict):
        out["keys"] = sorted(first.keys())
        out["null_keys"] = sorted(k for k, v in first.items() if v is None)
        # Lengths of list-valued fields (no values) — surfaces empty/thin records
        # (e.g. an empty animals/visits/technicians array) which signal a
        # degraded "empty" response distinct from a populated one.
        array_lens = {k: len(v) for k, v in first.items() if isinstance(v, list)}
        if array_lens:
            out["array_lens"] = array_lens
        # Keys whose value is an empty string — another empty-response signal.
        empty_str_keys = sorted(k for k, v in first.items() if v == "")
        if empty_str_keys:
            out["empty_str_keys"] = empty_str_keys
    return out


def _record_api_trace(observation, response, *, provider: str, url: str) -> None:
    """Attach status + a PII-safe response structure + source to a Langfuse
    observation. This is how we prove inconsistent upstream returns. Span latency
    is recorded by Langfuse from the observation duration. Raw bodies are only
    included when FARMER_API_TRACE_BODY is enabled (deep-debug). Wrapped in
    try/except: tracing must never break a read.
    """
    if observation is None:
        return
    try:
        body = response.text or ""
    except Exception:
        body = ""
    try:
        output = {
            "status_code": response.status_code,
            "ok": 200 <= response.status_code < 300,
            "fetch_reason": _fetch_reason.get(),
            **_safe_response_summary(body),
        }
        if settings.farmer_api_trace_body and settings.farmer_api_trace_body_chars > 0:
            output["body"] = body[: settings.farmer_api_trace_body_chars]
        observation.update(output=output, metadata={"provider": provider, "url": url})
    except Exception:
        pass


class GetAITechniciansBySocietyQueryParams(BaseModel):
    union_code: str = Field(..., alias="unionCode")
    society_code: str = Field(..., alias="societyCode")

    def to_query_params(self) -> dict[str, str]:
        return {
            "unionCode": self.union_code,
            "societyCode": self.society_code,
        }


class AITechnicianBySocietyRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    userId: Optional[str] = None
    fullName: Optional[str] = None
    mobileNumber: Optional[str] = None


def normalize_phone(mobile: str) -> str:
    """Strip non-digits; for Indian numbers optionally strip leading 91."""
    digits = re.sub(r"\D", "", mobile or "")
    if digits.startswith("91") and len(digits) > 10:
        digits = digits[2:].lstrip("0") or digits
    return digits.lstrip("0") or mobile or ""


def normalize_tag(tag_no: str) -> str:
    """Strip whitespace from tag number."""
    return (tag_no or "").strip()


def _parse_farmer_response(r: httpx.Response, provider: str) -> Optional[List[Dict[str, Any]]]:
    """Records, or None when upstream answered cleanly with no record.

    Raises BackendUnavailableError when upstream answered with an error status or
    a body we cannot read — the caller must not treat that as "no such farmer".
    """
    if r.status_code == 204:
        return None
    if r.status_code != 200:
        raise BackendUnavailableError(provider, f"HTTP {r.status_code}")
    if not (r.text or "").strip():
        return None
    try:
        data = json.loads(r.text)
    except json.JSONDecodeError as e:
        raise BackendUnavailableError(provider, f"unparseable body: {e}") from e
    if isinstance(data, list) and len(data) > 0:
        return data
    if isinstance(data, dict) and data.get("data") and isinstance(data["data"], list):
        return data["data"]
    return None


# --- Farmer ---


async def fetch_farmer_amulpashudhan(mobile: str, token: str) -> Optional[List[Dict[str, Any]]]:
    """Farmer records, or None when upstream says this mobile has no record.

    Raises BackendUnavailableError if upstream errored — never None for that.
    """
    url = f"{BASE_AMULPASHUDHAN}/GetFarmerDetailsByMobile?mobileNumber={mobile}"
    try:
        with start_observation(
            "fetch_farmer_amulpashudhan",
            input={"mobile": mobile},
            metadata={"provider": "amulpashudhan", "url": url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    url,
                    headers={"accept": "application/json", "Authorization": f"Bearer {token}"},
                )
            _record_api_trace(observation, r, provider="amulpashudhan", url=url)
    except Exception as e:
        raise BackendUnavailableError("amulpashudhan", f"request failed: {e}") from e
    return _parse_farmer_response(r, provider="amulpashudhan")


async def fetch_farmer_herdman(mobile: str, token: str) -> Optional[List[Dict[str, Any]]]:
    """Farmer records, or None when upstream says this mobile has no record.

    Raises BackendUnavailableError if upstream errored — never None for that.
    """
    url = f"{BASE_HERDMAN}/get-amul-farmer"
    try:
        with start_observation(
            "fetch_farmer_herdman",
            input={"mobile": mobile},
            metadata={"provider": "herdman", "url": url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    url,
                    params={"mobileno": mobile},
                    headers={"accept": "application/json", "api-token": f"Bearer {token}"},
                )
            _record_api_trace(observation, r, provider="herdman", url=url)
    except Exception as e:
        raise BackendUnavailableError("herdman", f"request failed: {e}") from e
    return _parse_farmer_response(r, provider="herdman")


def _farmer_record_key(rec: Dict[str, Any]) -> tuple:
    """Key for deduplication: societyName + farmerCode."""
    return (str(rec.get("societyName") or ""), str(rec.get("farmerCode") or ""))


def merge_farmer_records(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Deduplicate by societyName+farmerCode; drop entries that are all nulls."""
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for rec in records:
        if not rec:
            continue
        key = _farmer_record_key(rec)
        if key in seen:
            continue
        seen.add(key)
        out.append(rec)
    return out


# --- Animal ---


async def fetch_animal_amulpashudhan(tag_no: str, token: str) -> Optional[Dict[str, Any]]:
    """Returns single animal dict or None on 204/error/empty."""
    url = f"{BASE_AMULPASHUDHAN}/GetAnimalDetailsByTagNo?tagNo={tag_no}"
    try:
        with start_observation(
            "fetch_animal_amulpashudhan",
            input={"tag_no": tag_no},
            metadata={"provider": "amulpashudhan", "url": url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    url,
                    headers={"accept": "application/json", "Authorization": f"Bearer {token}"},
                )
            _record_api_trace(observation, r, provider="amulpashudhan", url=url)
        if r.status_code == 204 or not (r.text or "").strip():
            return None
        if r.status_code != 200:
            return None
        data = json.loads(r.text)
        if isinstance(data, dict) and data.get("tagNumber"):
            return data
        if isinstance(data, dict) and data.get("tagNo"):
            data["tagNumber"] = data["tagNo"]
            return data
        return None
    except (json.JSONDecodeError, httpx.HTTPError, Exception):
        return None


def _normalize_herdman_animal(raw: Dict[str, Any]) -> Dict[str, Any]:
    """Map herdman Animal item to canonical keys."""
    out: Dict[str, Any] = {}
    out["tagNumber"] = raw.get("tagno") or raw.get("tagNumber") or raw.get("TagID")
    out["animalType"] = raw.get("Animal Type") or raw.get("animalType")
    out["breed"] = raw.get("Breed") or raw.get("breed")
    out["milkingStage"] = raw.get("Milking Stage") or raw.get("milkingStage")
    out["pregnancyStage"] = raw.get("pregnancyStage")
    out["dateOfBirth"] = raw.get("DOB") or raw.get("dateOfBirth")
    out["lactationNo"] = raw.get("Currant Lactation no") if "Currant Lactation no" in raw else raw.get("lactationNo")
    out["lastBreedingActivity"] = raw.get("Last AI") or raw.get("lastBreedingActivity")
    out["lastHealthActivity"] = raw.get("lastHealthActivity")
    out["lastPD"] = raw.get("Last PD")
    out["lastCalvingDate"] = raw.get("Last Calvingdate")
    out["farmerComplaint"] = raw.get("Farmer complaint")
    out["diagnosis"] = raw.get("Diagnosis")
    out["medicineGiven"] = raw.get("Medicine Given")
    return {k: v for k, v in out.items() if v is not None}


async def fetch_animal_herdman(tag_no: str, token: str) -> Optional[Dict[str, Any]]:
    """Returns single animal dict (canonical keys) or None on error/empty."""
    url = f"{BASE_HERDMAN}/get-amul-animal"
    try:
        with start_observation(
            "fetch_animal_herdman",
            input={"tag_no": tag_no},
            metadata={"provider": "herdman", "url": url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(
                    url,
                    params={"TagID": tag_no},
                    headers={"accept": "application/json", "api-token": f"Bearer {token}"},
                )
            _record_api_trace(observation, r, provider="herdman", url=url)
        if r.status_code != 200 or not (r.text or "").strip():
            return None
        data = json.loads(r.text)
        if isinstance(data, dict) and data.get("Animal") and isinstance(data["Animal"], list) and len(data["Animal"]) > 0:
            return _normalize_herdman_animal(data["Animal"][0])
        if isinstance(data, list) and len(data) > 0 and isinstance(data[0], dict):
            return _normalize_herdman_animal(data[0])
        if isinstance(data, dict) and (data.get("tagno") or data.get("tagNumber")):
            return _normalize_herdman_animal(data)
        return None
    except (json.JSONDecodeError, httpx.HTTPError, Exception):
        return None


def merge_animal_data(primary: Optional[Dict], fallback: Optional[Dict]) -> Dict[str, Any]:
    """Merge primary (amulpashudhan) with fallback (herdman). Prefer primary; fill missing from fallback."""
    if primary and fallback:
        merged = dict(primary)
        for k, v in fallback.items():
            if v is not None and (merged.get(k) is None or merged.get(k) == ""):
                merged[k] = v
        return merged
    if primary:
        return primary
    if fallback:
        return fallback
    return {}


async def create_ai_call_api(
    request: AICallRequestModel, token: str
) -> AICallResponseModel | None:
    """Creates an artificial insemination call and returns the assigned technician."""
    api_url = f"{BASE_AMULPASHUDHAN}/CreateAICall"
    try:
        with start_observation(
            "create_ai_call_api",
            input=request.to_query_params(),
            metadata={"provider": "amulpashudhan", "url": api_url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    api_url,
                    params=request.to_query_params(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                _record_api_trace(observation, response, provider="amulpashudhan", url=api_url)
                response.raise_for_status()
                _logger.info(
                    "[CreateAICall(%s,%s,%s,%s)] :: Response received.",
                    request.union_code, request.society_code, request.farmer_code, request.species.value,
                )
        response_json = response.json()
        if not isinstance(response_json, dict):
            raise Exception("Not a valid dict in response.")
        return AICallResponseModel.model_validate(response_json)
    except httpx.HTTPStatusError as e:
        _logger.error("[CreateAICall] :: HTTP %s: %s", e.response.status_code, e.response.text)
    except Exception as e:
        _logger.error("[CreateAICall] :: Error: %s", e)
    return None


async def create_health_call_api(
    request: HealthCallRequestModel, token: str
) -> HealthCallResponseModel | None:
    """Creates a health call and returns the ticket details."""
    api_url = f"{BASE_AMULPASHUDHAN}/CreateHealthCall"
    try:
        with start_observation(
            "create_health_call_api",
            input=request.to_query_params(),
            metadata={"provider": "amulpashudhan", "url": api_url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    api_url,
                    params=request.to_query_params(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                _record_api_trace(observation, response, provider="amulpashudhan", url=api_url)
                response.raise_for_status()
                _logger.info(
                    "[CreateHealthCall(%s,%s,%s,%s,%s)] :: Response received.",
                    request.union_code,
                    request.society_code,
                    request.farmer_code,
                    request.species.value,
                    request.case_type.value,
                )
        response_json = response.json()
        if not isinstance(response_json, dict):
            raise Exception("Not a valid dict in response.")
        return HealthCallResponseModel.model_validate(response_json)
    except httpx.HTTPStatusError as e:
        _logger.error(
            "[CreateHealthCall(%s,%s,%s,%s,%s)] :: HTTP %s: %s",
            request.union_code,
            request.society_code,
            request.farmer_code,
            request.species.value,
            request.case_type.value,
            e.response.status_code,
            e.response.text,
        )
    except Exception as e:
        _logger.error(
            "[CreateHealthCall(%s,%s,%s,%s,%s)] :: Error: %s",
            request.union_code,
            request.society_code,
            request.farmer_code,
            request.species.value,
            request.case_type.value,
            e,
        )
    return None


async def get_ai_technicians_by_society_api(
    query: GetAITechniciansBySocietyQueryParams,
    token: str,
) -> list[AITechnicianBySocietyRecord] | None:
    """Fetch AI technicians mapped to a union and society."""
    api_url = f"{BASE_AMULPASHUDHAN}/GetAITUserDetailsBySocietyCode"
    try:
        with start_observation(
            "get_ai_technicians_by_society_api",
            input=query.to_query_params(),
            metadata={"provider": "amulpashudhan", "url": api_url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    api_url,
                    params=query.to_query_params(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                _record_api_trace(observation, response, provider="amulpashudhan", url=api_url)
                response.raise_for_status()

        response_json = response.json()
        if isinstance(response_json, dict) and isinstance(response_json.get("data"), list):
            response_json = response_json["data"]
        if not isinstance(response_json, list):
            raise ValueError("Expected list response from GetAITechniciansBySociety")

        return [AITechnicianBySocietyRecord.model_validate(item) for item in response_json if isinstance(item, dict)]
    except httpx.HTTPStatusError as e:
        _logger.error(
            "[GetAITechniciansBySociety(%s,%s)] :: HTTP %s: %s",
            query.union_code,
            query.society_code,
            e.response.status_code,
            e.response.text,
        )
    except Exception as e:
        _logger.error(
            "[GetAITechniciansBySociety(%s,%s)] :: Error: %s",
            query.union_code,
            query.society_code,
            e,
        )
    return None


async def get_farmer_milk_collection_details_api(
    request: FarmerMilkCollectionRequestModel,
    token: str,
) -> FarmerMilkCollectionResponseModel | None:
    """Fetches farmer milk collection and deduction details from PashuGPT."""
    api_url = f"{BASE_AMULPASHUDHAN}/FarmerMilkCollectionDetails"
    try:
        with start_observation(
            "get_farmer_milk_collection_details_api",
            input=request.to_query_params(),
            metadata={"provider": "amulpashudhan", "url": api_url},
        ) as observation:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(
                    api_url,
                    params=request.to_query_params(),
                    headers={"Authorization": f"Bearer {token}"},
                )
                _record_api_trace(observation, response, provider="amulpashudhan", url=api_url)
                response.raise_for_status()

        if response.status_code == 204 or not (response.text or "").strip():
            return None

        response_json = response.json()
        if not isinstance(response_json, dict):
            raise ValueError("Expected dict response from FarmerMilkCollectionDetails")

        return FarmerMilkCollectionResponseModel.model_validate(response_json)
    except httpx.HTTPStatusError as e:
        _logger.error(
            "[FarmerMilkCollectionDetails(%s,%s,%s,%s,%s)] :: HTTP %s: %s",
            request.union_code,
            request.society_code,
            request.farmer_code,
            request.fromdate,
            request.todate,
            e.response.status_code,
            e.response.text,
        )
    except Exception as e:
        _logger.error(
            "[FarmerMilkCollectionDetails(%s,%s,%s,%s,%s)] :: Error: %s",
            request.union_code,
            request.society_code,
            request.farmer_code,
            request.fromdate,
            request.todate,
            e,
        )
    return None
