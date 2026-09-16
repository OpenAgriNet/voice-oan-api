"""
PMFBY Grievance Tools (initiate_pmfby_grievance_otp, check_pmfby_grievance_otp,
pmfby_grievance_status, pmfby_submit_grievance)

Implements the Beckn BAP `/init` and `/status` flow for provider `pmfby-grievance`,
using the same transaction-id + OTP validation conventions as pmfby_scheme_status.py.

See pmkisan_grievance.py for the separate PM-KISAN grievance flow.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from helpers.utils import get_logger
from app.config import get_default_httpx_timeout
from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry
from pydantic_ai.tools import RunContext

from agents.deps import FarmerContext

logger = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

TICKET_CATEGORY_ID = "3"
TICKET_SUB_CATEGORY_ID = "10"
PMFBY_GRIEVANCE_RECEIPT_SOURCE_ID = "134306"

_OTP_FAILURE_SUBSTRINGS = (
    "invalid otp",
    "otp invalid",
    "wrong otp",
    "expired otp",
    "otp expired",
    "verification failed",
    "otp mismatch",
    "incorrect otp",
)


def _today_yyyy_mm_dd() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def generate_transaction_id(session_id: str, key: str) -> str:
    """Deterministic transaction id across OTP init, status verify, and grievance submit."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, (session_id + key)))


def normalize_phone_for_api(phone: str) -> str:
    """Digits only; 10-digit Indian mobile for BAP (no country code)."""
    digits = "".join(c for c in str(phone).strip() if c.isdigit())
    if len(digits) == 12 and digits.startswith("91"):
        return digits[2:]
    if len(digits) == 11 and digits.startswith("0"):
        return digits[1:]
    return digits if digits else str(phone).strip()


def _validate_otp(otp: str) -> str:
    otp_str = str(otp).strip() if otp else ""
    if not otp_str:
        raise ModelRetry("Invalid OTP. Please provide the 6-digit OTP received via SMS.")
    digits = "".join(c for c in otp_str if c.isdigit())
    if len(digits) != 6:
        raise ModelRetry("PMFBY OTP must be exactly 6 digits. Please ask for the 6-digit OTP received on mobile.")
    return digits


def _normalize_request_season_for_pmfby_api(season: str) -> str:
    """Map Kharif/Rabi/Summer (and 1-3) to PMFBY-style season ids for `request_season` tag."""
    raw = str(season).strip()
    if not raw:
        return raw
    low = raw.lower()
    try:
        n = int(raw)
        if 1 <= n <= 3:
            return str(n)
    except ValueError:
        pass
    if "kharif" in low:
        return "1"
    if "rabi" in low:
        return "2"
    if "summer" in low or "zaid" in low:
        return "3"
    return raw


def _require_nonempty(value: Optional[str], retry_message: str) -> str:
    s = str(value).strip() if value is not None else ""
    if not s:
        raise ModelRetry(retry_message)
    return s


# --------------------------------------------------------------------------------------
# Beckn transport
# --------------------------------------------------------------------------------------


def _bap_url(path: str) -> str:
    base = os.getenv("BAP_ENDPOINT", "").rstrip("/")
    if not base:
        raise ModelRetry("BAP endpoint is not configured. Set BAP_ENDPOINT.")
    return f"{base}/{path.lstrip('/')}"


def _beckn_context(*, transaction_id: str, action: str) -> Dict[str, Any]:
    return {
        "domain": "schemes:vistaar",
        "action": action,
        "version": "1.1.0",
        "bap_id": os.getenv("BAP_ID"),
        "bap_uri": os.getenv("BAP_URI"),
        "bpp_id": os.getenv("BPP_ID"),
        "bpp_uri": os.getenv("BPP_URI"),
        "transaction_id": transaction_id,
        "message_id": str(uuid.uuid4()),
        "timestamp": str(int(datetime.now(timezone.utc).timestamp())),
        "ttl": "PT10M",
        "location": {"country": {"code": "IND"}, "city": {"code": "*"}},
    }


def _post_json_logged(url: str, payload: Dict[str, Any], log_prefix: str) -> httpx.Response:
    logger.info(f"{log_prefix} Request URL: {url}")
    logger.info(f"{log_prefix} Request Payload: {json.dumps(payload, indent=2)}")
    response = httpx.post(url, json=payload, timeout=get_default_httpx_timeout())
    logger.info(f"{log_prefix} Response Status: {response.status_code}")
    logger.info(f"{log_prefix} Response Payload: {response.text}")
    return response


# --------------------------------------------------------------------------------------
# Request payloads
# --------------------------------------------------------------------------------------


def _payload_get_otp_grievance_flow(*, transaction_id: str, phone: str) -> Dict[str, Any]:
    return {
        "context": _beckn_context(transaction_id=transaction_id, action="init"),
        "message": {
            "order": {
                "provider": {"id": "pmfby-agri"},
                "items": [{"id": "pmfby"}],
                "fulfillments": [
                    {
                        "customer": {
                            "person": {
                                "tags": [
                                    {"descriptor": {"code": "request_type"}, "value": "get_otp"},
                                    {"descriptor": {"code": "phone_number"}, "value": phone},
                                ]
                            },
                            "contact": {"phone": phone},
                        }
                    }
                ],
            }
        },
    }


def _payload_grievance_status(*, transaction_id: str, phone: str, grievance_support_ticket_no: str) -> Dict[str, Any]:
    """Beckn `/status` for PMFBY grievance ticket lookup (`status_grievance`)."""
    ticket = str(grievance_support_ticket_no).strip()
    return {
        "context": _beckn_context(transaction_id=transaction_id, action="status"),
        "message": {
            "order_id": ticket,
            "order": {
                "provider": {"id": "pmfby-grievance"},
                "items": [{"id": "pmfby-grievance"}],
                "fulfillments": [
                    {
                        "customer": {
                            "person": {
                                "tags": [
                                    {"descriptor": {"code": "request_type"}, "value": "status_grievance"},
                                    {"descriptor": {"code": "requestorMobileNo"}, "value": phone},
                                    {
                                        # API field name (typo preserved for BAP compatibility).
                                        "descriptor": {"code": "GrievenceSupportTicketNo"},
                                        "value": ticket,
                                    },
                                ]
                            }
                        }
                    }
                ],
            },
        },
    }


def _payload_status_verify_otp(*, transaction_id: str, otp: str, phone: str) -> Dict[str, Any]:
    return {
        "context": _beckn_context(transaction_id=transaction_id, action="status"),
        "message": {
            "order_id": otp,
            "order": {
                "id": "order-1",
                "provider": {"id": "pmfby-agri"},
                "items": [{"id": "pmfby"}],
                "fulfillments": [{"customer": {"contact": {"phone": phone}}}],
            },
        },
    }


class PMfbyGrievanceInitRequest(BaseModel):
    """Beckn `/init` body for `pmfby-grievance` submit."""

    transaction_id: str
    phone_number: str
    complaint_date: str = Field(default_factory=_today_yyyy_mm_dd)
    receipt_source_id: str
    ticket_category_id: str = TICKET_CATEGORY_ID
    ticket_sub_category_id: str = TICKET_SUB_CATEGORY_ID
    request_year: str
    request_season: str
    application_no: str
    grievance_description: str

    def get_payload(self) -> Dict[str, Any]:
        phone = normalize_phone_for_api(self.phone_number)
        tags: List[Dict[str, Any]] = [
            ("request_type", "submit_grievance"),
            ("phone_number", phone),
            ("complaint_date", str(self.complaint_date)),
            ("receipt_source_id", str(self.receipt_source_id)),
            ("ticket_category_id", str(self.ticket_category_id)),
            ("ticket_sub_category_id", str(self.ticket_sub_category_id)),
            ("request_year", str(self.request_year)),
            ("request_season", str(self.request_season)),
            ("application_no", str(self.application_no)),
            ("grievance_description", str(self.grievance_description)),
        ]
        tag_objs = [{"descriptor": {"code": code}, "value": val} for code, val in tags]
        return {
            "context": _beckn_context(transaction_id=self.transaction_id, action="init"),
            "message": {
                "order": {
                    "provider": {"id": "pmfby-grievance"},
                    "items": [{"id": "pmfby-grievance"}],
                    "fulfillments": [{"customer": {"person": {"tags": tag_objs}}}],
                }
            },
        }


# --------------------------------------------------------------------------------------
# Response parsing (grievance submit / status)
# --------------------------------------------------------------------------------------


class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None


class TagListItem(BaseModel):
    descriptor: Optional[Descriptor] = None
    value: Optional[str] = None
    display: bool = True


class Tag(BaseModel):
    descriptor: Optional[Descriptor] = None
    list: Optional[List[TagListItem]] = None
    display: bool = True


class InitOrder(BaseModel):
    tags: Optional[List[Tag]] = None


class InitMessage(BaseModel):
    order: Optional[InitOrder] = None


class InitResponseItem(BaseModel):
    message: Optional[InitMessage] = None


class InitResponse(BaseModel):
    responses: List[InitResponseItem] = []

    def format_grievance_result(self) -> str:
        if not self.responses:
            return "PMFBY grievance service returned no response. Please try again."

        for r in self.responses:
            order = (r.message.order if r.message and r.message.order else None) if r.message else None
            if not order or not order.tags:
                continue
            for tag in order.tags:
                if not tag.display:
                    continue
                tag_code = (tag.descriptor.code if tag.descriptor else None) or ""
                if tag_code != "grievance-response" or not tag.list:
                    continue

                kv: Dict[str, str] = {}
                for li in tag.list:
                    if not li.display or not li.descriptor or not li.descriptor.code or li.value is None:
                        continue
                    kv[str(li.descriptor.code)] = str(li.value)

                status = kv.get("status")
                ticket_no = kv.get("ticket-no") or kv.get("ticket_no")
                ticket_id = kv.get("ticket-id") or kv.get("ticket_id")
                message = kv.get("message")

                parts: List[str] = []
                if status:
                    parts.append(f"Status: {status}")
                if ticket_no:
                    parts.append(f"Ticket Number: {ticket_no}")
                if ticket_id:
                    parts.append(f"Ticket ID: {ticket_id}")
                if message:
                    parts.append(f"Message: {message}")
                return "\n".join(parts) if parts else "PMFBY grievance submitted. Please note the ticket details shared by the system."

        return "PMFBY grievance submitted, but ticket details were not found in the response."

    def format_status_result(self) -> str:
        """Format grievance status from `/status` (status_grievance) responses."""
        if not self.responses:
            return "No grievance status found for this ticket."

        lines: List[str] = []
        for r in self.responses:
            order = (r.message.order if r.message and r.message.order else None) if r.message else None
            if not order or not order.tags:
                continue
            for tag in order.tags:
                if not tag.display:
                    continue
                if tag.list:
                    for li in tag.list:
                        if not li.display or not li.descriptor or li.value is None:
                            continue
                        name = li.descriptor.name or li.descriptor.code or "Detail"
                        lines.append(f"{name}: {li.value}")
                elif tag.descriptor and tag.value is not None:
                    name = tag.descriptor.name or tag.descriptor.code or "Detail"
                    lines.append(f"{name}: {tag.value}")

        if lines:
            return "\n".join(lines)
        return "No grievance status found for this ticket."


# --------------------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------------------


def initiate_pmfby_grievance_otp(ctx: RunContext[FarmerContext], phone_number: str) -> str:
    """Send OTP to the farmer's registered mobile for PMFBY grievance (`get_otp` /init).

    Args:
        phone_number (str): Phone number registered with PMFBY (10 digits).

    Returns:
        str: OTP sent confirmation, or an error message.
    """
    try:
        transaction_id = generate_transaction_id(ctx.deps.session_id, phone_number)
        phone = normalize_phone_for_api(phone_number)
        payload = _payload_get_otp_grievance_flow(transaction_id=transaction_id, phone=phone)
        url = _bap_url("init")
        response = _post_json_logged(url, payload, "[PMFBY GRIEVANCE OTP INIT]")

        if response.status_code != 200:
            return f"PMFBY OTP init service unavailable. Status code: {response.status_code}"

        return "OTP has been sent to the registered mobile number. Please share the 6-digit OTP to proceed with grievance lodging."

    except httpx.TimeoutException:
        return "PMFBY OTP init request timed out. Please try again later."
    except httpx.RequestError as e:
        return f"PMFBY OTP init request failed: {str(e)}"
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"initiate_pmfby_grievance_otp: {e}")
        raise ModelRetry(f"Unexpected error in PMFBY OTP init request. {str(e)}") from e


def check_pmfby_grievance_otp(ctx: RunContext[FarmerContext], otp: str, phone_number: str) -> str:
    """Verify OTP via `/status` (order_id = OTP, phone-only fulfillments) after initiate_pmfby_grievance_otp.

    Args:
        otp (str): 6-digit OTP received via SMS.
        phone_number (str): Phone number registered with PMFBY (10 digits).

    Returns:
        str: Verification result. HTTP 200 with no OTP-failure phrases means success.
    """
    try:
        otp_str = _validate_otp(otp)
        transaction_id = generate_transaction_id(ctx.deps.session_id, phone_number)
        phone = normalize_phone_for_api(phone_number)
        payload = _payload_status_verify_otp(transaction_id=transaction_id, otp=otp_str, phone=phone)
        url = _bap_url("status")
        response = _post_json_logged(url, payload, "[PMFBY GRIEVANCE OTP STATUS]")

        if response.status_code != 200:
            return f"OTP verification failed (service unavailable). Status code: {response.status_code}"

        blob = response.text.lower()
        if any(s in blob for s in _OTP_FAILURE_SUBSTRINGS):
            return "OTP verification failed. Please re-check the OTP and try again."

        return "OTP verified. Please share your PMFBY application number, policy year, season, and a brief grievance description."

    except httpx.TimeoutException:
        return "OTP verification request timed out. Please try again."
    except httpx.RequestError as e:
        return f"OTP verification request failed: {str(e)}"
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"check_pmfby_grievance_otp: {e}")
        raise ModelRetry(f"Unexpected error during OTP verification. {str(e)}") from e


def pmfby_grievance_status(
    ctx: RunContext[FarmerContext],
    phone_number: str,
    grievance_support_ticket_no: str,
) -> str:
    """Check PMFBY grievance status for an existing support ticket. No OTP required.

    Args:
        phone_number (str): Mobile number registered with PMFBY (10 digits).
        grievance_support_ticket_no (str): Grievance support ticket number.

    Returns:
        str: Grievance status details from the PMFBY grievance portal.
    """
    try:
        phone = normalize_phone_for_api(
            _require_nonempty(phone_number, "Please share your registered mobile number.")
        )
        if len(phone) != 10:
            raise ModelRetry("Please share a valid 10-digit mobile number registered with PMFBY.")

        ticket = _require_nonempty(
            grievance_support_ticket_no,
            "Please share your PMFBY grievance support ticket number.",
        )

        transaction_id = generate_transaction_id(ctx.deps.session_id, f"{phone}:{ticket}")
        payload = _payload_grievance_status(
            transaction_id=transaction_id, phone=phone, grievance_support_ticket_no=ticket
        )
        url = _bap_url("status")
        response = _post_json_logged(url, payload, "[PMFBY GRIEVANCE STATUS]")

        if response.status_code != 200:
            return f"PMFBY grievance status service unavailable. Status code: {response.status_code}"

        response_text = response.text.strip()
        if not response_text:
            return "PMFBY grievance status service returned an empty response. Please try again."

        try:
            response_json = response.json()
        except json.JSONDecodeError:
            return "PMFBY grievance status service returned a non-JSON response. Please try again."

        return InitResponse.model_validate(response_json).format_status_result()

    except httpx.TimeoutException:
        return "PMFBY grievance status request timed out. Please try again later."
    except httpx.RequestError as e:
        return f"PMFBY grievance status request failed: {str(e)}"
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"pmfby_grievance_status: {e}")
        raise ModelRetry(f"Unexpected error checking PMFBY grievance status. {str(e)}") from e


def pmfby_submit_grievance(
    ctx: RunContext[FarmerContext],
    otp: str,
    phone_number: str,
    request_year: str,
    request_season: str,
    application_no: str,
    grievance_description: str,
) -> str:
    """Submit PMFBY grievance via Beckn `/init` (pmfby-grievance) after OTP verification.

    Call initiate_pmfby_grievance_otp then check_pmfby_grievance_otp first; use this
    tool only after the farmer shares the OTP.

    Args:
        otp (str): 6-digit OTP received via SMS and already verified via check_pmfby_grievance_otp.
        phone_number (str): Phone number registered with PMFBY (10 digits).
        request_year (str): Policy year.
        request_season (str): Kharif, Rabi, or Summer.
        application_no (str): PMFBY application number.
        grievance_description (str): Description of the grievance (at least 10 characters).

    Returns:
        str: Grievance submission result with ticket details, or an error message.
    """
    try:
        _ = _validate_otp(otp)
        _require_nonempty(phone_number, "Please share your registered mobile number.")
        request_year = _require_nonempty(request_year, "Please share the request year.")
        raw_season = _require_nonempty(request_season, "Please share the request season.")

        season_api = _normalize_request_season_for_pmfby_api(raw_season)
        if season_api not in ("1", "2", "3"):
            raise ModelRetry(
                "Ask which crop season the PMFBY policy is for: Kharif, Rabi, or Summer. "
                "Do not ask them to memorize number codes; pass the season name to this tool."
            )

        application_no = _require_nonempty(application_no, "Please share your PMFBY application number.")

        if not grievance_description or len(grievance_description.strip()) < 10:
            raise ModelRetry("Please provide a brief grievance description (at least 10 characters).")

        transaction_id = generate_transaction_id(ctx.deps.session_id, phone_number)
        payload = PMfbyGrievanceInitRequest(
            transaction_id=transaction_id,
            phone_number=phone_number,
            # PMFBY grievance receipt_source_id is intentionally hardcoded per integration requirement.
            receipt_source_id=PMFBY_GRIEVANCE_RECEIPT_SOURCE_ID,
            request_year=request_year.strip(),
            request_season=season_api,
            application_no=application_no.strip(),
            grievance_description=grievance_description.strip(),
        ).get_payload()

        url = _bap_url("init")
        response = _post_json_logged(url, payload, "[PMFBY GRIEVANCE SUBMIT]")

        if response.status_code != 200:
            return f"PMFBY grievance service unavailable. Status code: {response.status_code}"

        response_text = response.text.strip()
        if not response_text:
            return "PMFBY grievance submitted. Please wait for the ticket details."

        try:
            response_json = response.json()
        except json.JSONDecodeError:
            return "PMFBY grievance submitted, but the service returned a non-JSON response."

        return InitResponse.model_validate(response_json).format_grievance_result()

    except httpx.TimeoutException:
        return "PMFBY grievance request timed out. Please try again later."
    except httpx.RequestError as e:
        return f"PMFBY grievance request failed: {str(e)}"
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"pmfby_submit_grievance: {e}")
        raise ModelRetry(f"Unexpected error in PMFBY grievance submission. {str(e)}") from e
