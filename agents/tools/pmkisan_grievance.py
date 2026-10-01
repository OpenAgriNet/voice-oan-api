"""
PM-KISAN Grievance Tools (pmkisan_grievance_send_otp, pmkisan_submit_grievance, pmkisan_grievance_status)

OTP-first flow via the Beckn BAP `/init` and `/status` endpoints for provider `pmkisan`,
using the same transaction-id + OTP validation conventions as pmkisan_scheme_status.py.

See pmfby_grievance.py for the separate PMFBY grievance flow.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

import httpx
from app.config import get_default_httpx_timeout
from pydantic import BaseModel, Field, ValidationError
from pydantic_ai import ModelRetry
from pydantic_ai.tools import RunContext

from agents.deps import FarmerContext
from agents.tools.pmkisan_scheme_status import (
    SchemeInitResponse,
    SchemeStatusResponse,
)
from helpers.utils import get_logger

logger = get_logger(__name__)

# --------------------------------------------------------------------------------------
# Config / Constants
# --------------------------------------------------------------------------------------

BAP_ID = os.getenv("BAP_ID")
BAP_URI = os.getenv("BAP_URI")
BPP_ID = os.getenv("BPP_ID")
BPP_URI = os.getenv("BPP_URI")

# Mapping file: human-friendly grievance labels -> backend codes
_DEFAULT_GRIEVANCE_JSON_PATH = (
    Path(__file__).resolve().parents[2] / "assets" / "grievance_types.json"
)
_GRIEVANCE_JSON_PATH = os.getenv("GRIEVANCE_TYPES_PATH", str(_DEFAULT_GRIEVANCE_JSON_PATH))


def _load_grievance_mapping(path: str) -> Dict[str, str]:
    try:
        p = Path(path)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("grievance_types.json must be an object of {label: code}")
            return {str(k): str(v) for k, v in data.items()}
    except Exception as e:
        logger.error(f"Failed to load grievance mapping at '{path}': {e}")
        return {}


GRIEVANCE_MAPPING: Dict[str, str] = _load_grievance_mapping(_GRIEVANCE_JSON_PATH)
GRIEVANCE_TYPES: List[str] = list(GRIEVANCE_MAPPING.keys())
# Literal built from the actual enum values so the LLM's tool schema exposes the
# exact valid strings instead of a plain str that invites paraphrased guesses.
GrievanceTypeLiteral = Literal[tuple(GRIEVANCE_TYPES)] if GRIEVANCE_TYPES else str

_REGISTRATION_NUMBER_LABEL = "Registration Number"
_BAP_ENDPOINT_NOT_CONFIGURED_MSG = "BAP_ENDPOINT is not configured in environment."

# -----------------------
# Response formatting
# -----------------------

def _format_http_response_raw(response: httpx.Response, *, max_chars: int = 8000) -> str:
    """Return the network response as a string (prefer pretty JSON), truncated to max_chars."""
    try:
        try:
            body = json.dumps(response.json(), ensure_ascii=False, indent=2)
        except Exception:
            body = response.text
        body = (body or "").strip()
        if len(body) > max_chars:
            body = body[:max_chars] + "\n... (truncated)"
        return body
    except Exception:
        # Last-resort: never crash while formatting tool output
        return (response.text or "").strip()


class GatewayContext(BaseModel):
    action: Optional[str] = None
    timestamp: Optional[str] = None
    message_id: Optional[str] = None
    transaction_id: Optional[str] = None
    ttl: Optional[str] = None
    domain: Optional[str] = None
    version: Optional[str] = None
    bap_id: Optional[str] = None
    bap_uri: Optional[str] = None
    bpp_id: Optional[str] = None
    bpp_uri: Optional[str] = None
    location: Optional[Dict[str, Any]] = None


class TagDescriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None


class TagListItem(BaseModel):
    descriptor: Optional[TagDescriptor] = None
    value: Optional[str] = None
    display: Optional[bool] = None


class TagGroup(BaseModel):
    descriptor: Optional[TagDescriptor] = None
    list: Optional[List[TagListItem]] = None
    display: Optional[bool] = None


class ProviderRef(BaseModel):
    id: Optional[str] = None


class ItemRef(BaseModel):
    id: Optional[str] = None


class OrderOut(BaseModel):
    provider: Optional[ProviderRef] = None
    items: Optional[List[ItemRef]] = None
    tags: Optional[List[TagGroup]] = None
    type: Optional[str] = None


class CatalogItem(BaseModel):
    id: Optional[str] = None
    descriptor: Optional[TagDescriptor] = None
    tags: Optional[List[TagGroup]] = None


class CatalogProvider(BaseModel):
    id: Optional[str] = None
    descriptor: Optional[TagDescriptor] = None
    items: Optional[List[CatalogItem]] = None


class CatalogOut(BaseModel):
    descriptor: Optional[TagDescriptor] = None
    providers: Optional[List[CatalogProvider]] = None


class MessageOut(BaseModel):
    order: Optional[OrderOut] = None
    catalog: Optional[CatalogOut] = None


class GatewaySubResponse(BaseModel):
    context: Optional[GatewayContext] = None
    message: Optional[MessageOut] = None


class GatewayResponse(BaseModel):
    context: Optional[GatewayContext] = None
    responses: Optional[List[GatewaySubResponse]] = None
    error: Optional[Any] = None

    @staticmethod
    def _add_tag_values(out: Dict[str, str], tag_groups: Optional[List[TagGroup]]) -> None:
        if not tag_groups:
            return

        for group in tag_groups:
            if not group or not group.list:
                continue
            for item in group.list:
                if not item or not item.descriptor or not item.descriptor.code:
                    continue
                if item.value is None:
                    continue
                out[str(item.descriptor.code)] = str(item.value)

    def _response_tags_kv(self) -> Dict[str, str]:
        out: Dict[str, str] = {}

        for sub in self.responses or []:
            if not sub or not sub.message:
                continue

            if sub.message.order:
                self._add_tag_values(out, sub.message.order.tags)

            catalog = sub.message.catalog
            if not catalog or not catalog.providers:
                continue
            for provider in catalog.providers:
                if not provider or not provider.items:
                    continue
                for item in provider.items:
                    if item:
                        self._add_tag_values(out, item.tags)

        return out

    @staticmethod
    def _format_details(details: str) -> List[str]:
        details = details.strip()
        if not details:
            return []

        try:
            parsed = json.loads(details)
        except Exception:
            return [f"Details: {details}"]

        records = parsed if isinstance(parsed, list) else [parsed]
        lines: List[str] = []
        field_labels = {
            "Farmer_Name": "Farmer Name",
            "Father_Name": "Father Name",
            "Gender": "Gender",
            "Reg_No": _REGISTRATION_NUMBER_LABEL,
            "StateName": "State",
            "DistrictName": "District",
            "BlockName": "Block",
            "RevenueVillageName": "Village",
            "GrievanceDate": "Grievance Date",
            "GrievanceStatus": "Grievance Status",
            "OfficerReply": "Officer Reply",
            "OfficeReplyDate": "Office Reply Date",
            "MobileNo": "Mobile No",
            "GrievanceDescription": "Grievance Description",
        }

        for index, record in enumerate(records, start=1):
            if not isinstance(record, dict):
                continue
            if len(records) > 1:
                lines.append(f"Record {index}:")
            for field, label in field_labels.items():
                value = record.get(field)
                if value is not None and str(value).strip():
                    lines.append(f"{label}: {str(value).strip()}")

        return lines

    def __str__(self) -> str:
        # Mirror other tools: readable summary from validated JSON
        lines: List[str] = []
        tx = self.context.transaction_id if self.context else None
        mid = self.context.message_id if self.context else None
        if tx:
            lines.append(f"transaction_id: {tx}")
        if mid:
            lines.append(f"message_id: {mid}")

        tags = self._response_tags_kv()
        if tags:
            # These codes match what the gateway returns in your sample
            if "status" in tags:
                lines.append(f"Status: {tags.get('status', '').strip()}")
            if "grievance-id" in tags:
                lines.append(f"Grievance ID: {tags.get('grievance-id', '').strip()}")
            if "lookup-type" in tags:
                lines.append(f"Lookup Type: {tags.get('lookup-type', '').strip()}")
            if "identity-no" in tags:
                lines.append(f"Identity No: {tags.get('identity-no', '').strip()}")
            if "grievance-type" in tags:
                lines.append(f"Grievance Type: {tags.get('grievance-type', '').strip()}")
            if "grievance-status" in tags:
                grievance_status = tags.get("grievance-status", "").strip() or "Not available"
                lines.append(f"Grievance Status: {grievance_status}")
            if "grievance-date" in tags:
                lines.append(f"Grievance Date: {tags.get('grievance-date', '').strip()}")
            if "message" in tags and tags["message"]:
                lines.append(f"Message: {tags.get('message', '').strip()}")
            if "details" in tags:
                lines.extend(self._format_details(tags["details"]))

        if self.error is not None:
            lines.append(f"Error: {json.dumps(self.error, ensure_ascii=False)}")

        if not lines:
            return "Request accepted."
        return "\n".join([l for l in lines if l.strip()]).strip()


def _format_grievance_response_formatted(response: httpx.Response) -> str:
    """Format the response the same way other tools do: parse JSON -> validate -> return str(model)."""
    resp_text = (response.text or "").strip()
    if not resp_text:
        return "Service returned empty response. Please try again later."

    try:
        parsed = response.json()
    except Exception:
        return resp_text

    try:
        model = GatewayResponse.model_validate(parsed)
        return str(model)
    except ValidationError:
        # If response shape evolves, don't break the tool — fallback to pretty JSON
        try:
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except Exception:
            return resp_text


def _format_otp_init_response(response: httpx.Response) -> str:
    resp_text = (response.text or "").strip()
    if not resp_text:
        return "OTP has been sent to the registered mobile number. Please provide the OTP to continue."

    try:
        model = SchemeInitResponse.model_validate(response.json())
        formatted = str(model).strip()
        return formatted or "OTP has been sent to the registered mobile number. Please provide the OTP to continue."
    except Exception:
        return _format_http_response_raw(response)


def _generate_pm_kisan_otp_transaction_id(session_id: str, reg_no: str) -> str:
    """Match the PM-KISAN status OTP transaction id without changing that tool's payload builders."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, session_id + reg_no))


def _build_pm_kisan_otp_context(action: Literal["init", "status"], transaction_id: str) -> Dict[str, Any]:
    return {
        "domain": "schemes:vistaar",
        "action": action,
        "version": "1.1.0",
        "bap_id": BAP_ID,
        "bap_uri": BAP_URI,
        "bpp_id": BPP_ID,
        "bpp_uri": BPP_URI,
        "transaction_id": transaction_id,
        "message_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z',
        "ttl": "PT10M",
        "location": {
            "country": {"code": "IND"},
            "city": {"code": "*"},
        },
    }


def _build_pm_kisan_otp_init_payload(reg_no: str, transaction_id: str) -> Dict[str, Any]:
    return {
        "context": _build_pm_kisan_otp_context("init", transaction_id),
        "message": {
            "order": {
                "provider": {"id": "pmkisan"},
                "items": [{"id": "pmkisan"}],
                "fulfillments": [
                    {
                        "customer": {
                            "person": {
                                "name": "Farmer",
                                "tags": [
                                    {
                                        "descriptor": {
                                            "name": "Registration Details",
                                            "code": "reg-details",
                                        },
                                        "list": [
                                            {
                                                "descriptor": {
                                                    "name": _REGISTRATION_NUMBER_LABEL,
                                                    "code": "reg-number",
                                                },
                                                "value": reg_no,
                                                "display": True,
                                            }
                                        ],
                                    }
                                ],
                            }
                        }
                    }
                ],
            }
        },
    }


def _build_pm_kisan_otp_status_payload(
    reg_no: str,
    otp: str,
    transaction_id: str,
    phone_number: str,
) -> Dict[str, Any]:
    return {
        "context": _build_pm_kisan_otp_context("status", transaction_id),
        "message": {
            "order_id": otp,
            "registration_number": reg_no,
            "phone_number": phone_number,
        },
    }


def _is_otp_verified(response_model: SchemeStatusResponse) -> bool:
    for response_item in response_model.responses or []:
        order = response_item.message.order
        if (order.state or "").upper() == "COMPLETED":
            return True

        for fulfillment in order.fulfillments or []:
            descriptor = fulfillment.state.descriptor if fulfillment.state else None
            if not descriptor:
                continue

            code = (descriptor.code or "").lower()
            short_desc = (descriptor.short_desc or "").lower()
            if code == "completed" or "otp verified" in short_desc:
                return True

    return False


def _request_pm_kisan_otp(
    ctx: RunContext[FarmerContext],
    reg_no: str,
    phone_number: str,
    purpose: str,
) -> str:
    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        raise ModelRetry(_BAP_ENDPOINT_NOT_CONFIGURED_MSG)

    reg_no_clean = str(reg_no).strip()
    transaction_id = _generate_pm_kisan_otp_transaction_id(ctx.deps.session_id, reg_no_clean)
    payload = _build_pm_kisan_otp_init_payload(reg_no_clean, transaction_id)

    endpoint = f"{bap_endpoint.rstrip('/')}/init"
    logger.info(f"[PM KISAN GRIEVANCE OTP INIT] Request URL: {endpoint}")
    logger.info(f"[PM KISAN GRIEVANCE OTP INIT] Payload: {json.dumps(payload)}")

    response = httpx.post(endpoint, json=payload, timeout=get_default_httpx_timeout())

    logger.info(f"[PM KISAN GRIEVANCE OTP INIT] Response Status: {response.status_code}")
    logger.info(f"[PM KISAN GRIEVANCE OTP INIT] Response Body: {response.text[:500]}")

    if response.status_code != 200:
        return f"Unable to send OTP for PM-KISAN grievance {purpose}. {_format_http_response_raw(response)}"

    formatted = _format_otp_init_response(response).strip()
    return f"{formatted}\nPlease provide the 4-digit OTP to {purpose}."


def _verify_pm_kisan_otp(
    ctx: RunContext[FarmerContext],
    reg_no: str,
    otp: str,
    phone_number: str,
) -> Optional[str]:
    otp_clean = str(otp).strip()
    if not otp_clean.isdigit() or len(otp_clean) != 4:
        return "Invalid OTP format. Please provide the 4-digit OTP received via SMS."

    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        raise ModelRetry(_BAP_ENDPOINT_NOT_CONFIGURED_MSG)

    reg_no_clean = str(reg_no).strip()
    transaction_id = _generate_pm_kisan_otp_transaction_id(ctx.deps.session_id, reg_no_clean)
    payload = _build_pm_kisan_otp_status_payload(
        reg_no_clean,
        otp_clean,
        transaction_id,
        str(phone_number).strip() if phone_number else "",
    )

    endpoint = f"{bap_endpoint.rstrip('/')}/status"
    logger.info(f"[PM KISAN GRIEVANCE OTP VERIFY] Request URL: {endpoint}")
    logger.info(f"[PM KISAN GRIEVANCE OTP VERIFY] Payload: {json.dumps(payload)}")

    response = httpx.post(endpoint, json=payload, timeout=get_default_httpx_timeout())

    logger.info(f"[PM KISAN GRIEVANCE OTP VERIFY] Response Status: {response.status_code}")
    logger.info(f"[PM KISAN GRIEVANCE OTP VERIFY] Response Body: {response.text[:500]}")

    if response.status_code != 200:
        return f"OTP verification failed. {_format_http_response_raw(response)}"

    resp_text = (response.text or "").strip()
    if not resp_text:
        return "OTP verification failed because the service returned an empty response. Please try again."

    try:
        response_model = SchemeStatusResponse.model_validate(response.json())
    except Exception:
        return f"OTP verification failed. {_format_http_response_raw(response)}"

    if _is_otp_verified(response_model):
        return None

    response_summary = str(response_model).strip()
    if response_summary:
        return f"OTP verification was not successful.\n{response_summary}"
    return "OTP verification was not successful. Please check the OTP and try again."

# -----------------------
# Shared Helpers
# -----------------------

def generate_transaction_id(session_id: str, identifier: str) -> str:
    """Generate a stable transaction ID for a grievance flow."""
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, (session_id + identifier + "grievance")))

# -----------------------
# Vistaar Models
# -----------------------

class Descriptor(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None
    short_desc: Optional[str] = None

class TagItem(BaseModel):
    descriptor: Descriptor
    value: Optional[str] = None
    display: bool = True

class Tag(BaseModel):
    display: bool = True
    descriptor: Descriptor
    list: Optional[List[TagItem]] = None

class Person(BaseModel):
    name: Optional[str] = None
    tags: Optional[List[Tag]] = None

class Contact(BaseModel):
    phone: Optional[str] = None

class Customer(BaseModel):
    person: Optional[Person] = None
    contact: Optional[Contact] = None

class Fulfillment(BaseModel):
    customer: Optional[Customer] = None

class Item(BaseModel):
    id: str

class Provider(BaseModel):
    id: str

class Order(BaseModel):
    provider: Provider
    items: List[Item]
    fulfillments: Optional[List[Fulfillment]] = None

class Context(BaseModel):
    domain: str = "schemes:vistaar"
    action: str
    version: str = "1.1.0"
    bap_id: Optional[str] = BAP_ID
    bap_uri: Optional[str] = BAP_URI
    bpp_id: Optional[str] = BPP_ID
    bpp_uri: Optional[str] = BPP_URI
    transaction_id: str
    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.%f')[:-3] + 'Z')
    location: Dict[str, Any] = {
        "country": {"code": "IND"},
        "city": {"code": "*"}
    }

class GrievanceInitRequest(BaseModel):
    context: Context
    message: Dict[str, Any]

    @classmethod
    def build(
        cls,
        transaction_id: str,
        identifier_value: str,
        identifier_type: Literal["reg-number"],
        grievance_type_code: str,
        grievance_description: str,
        customer_name: str = "Customer Name",
        phone: str = "",
    ) -> "GrievanceInitRequest":
        return cls(
            context=Context(action="init", transaction_id=transaction_id),
            message={
                "order": {
                    "provider": {"id": "pmkisan-greviance"},
                    "items": [{"id": "pmkisan-greviance"}],
                    "fulfillments": [
                        {
                            "customer": {
                                "person": {
                                    "name": customer_name,
                                    "tags": [
                                        {
                                            "display": True,
                                            "descriptor": {"name": "Registration Details", "code": "reg-details"},
                                            "list": [
                                                {
                                                    "descriptor": {"name": _REGISTRATION_NUMBER_LABEL, "code": identifier_type},
                                                    "value": identifier_value,
                                                    "display": True
                                                }
                                            ]
                                        },
                                        {
                                            "display": True,
                                            "descriptor": {"name": "Grievance Details", "code": "grievance-details"},
                                            "list": [
                                                {
                                                    "descriptor": {"name": "Grievance Type", "code": "grievance-type"},
                                                    "value": grievance_type_code,
                                                    "display": True
                                                },
                                                {
                                                    "descriptor": {"name": "Grievance Description", "code": "grievance-description"},
                                                    "value": grievance_description,
                                                    "display": True
                                                }
                                            ]
                                        }
                                    ]
                                },
                                "contact": {"phone": phone} if phone else {}
                            }
                        }
                    ]
                }
            }
        )


def _format_grievance_tool_response(response: httpx.Response, raw: bool) -> str:
    if raw:
        return _format_http_response_raw(response)
    return _format_grievance_response_formatted(response)


def _validate_submit_grievance_inputs(
    reg_no: str,
    grievance_type: str,
    grievance_description: str,
    otp: Optional[str],
) -> tuple[str, str]:
    """Return (identifier_value, reg_no_clean) or raise ModelRetry."""
    if not reg_no or not reg_no.strip():
        raise ModelRetry("Please provide the PM-KISAN Registration Number.")
    identifier_value = str(reg_no).strip()
    if not grievance_type or grievance_type not in GRIEVANCE_MAPPING:
        choices = '", "'.join(GRIEVANCE_TYPES)
        raise ModelRetry(f'Invalid grievance type: "{grievance_type}". Please select from: "{choices}".')
    if not grievance_description or len(grievance_description.strip()) < 10:
        raise ModelRetry("Please provide a brief grievance description (at least 10 characters).")
    reg_no_clean = identifier_value
    if not otp or not str(otp).strip():
        raise ModelRetry(
            "OTP verification is required before submitting a grievance. "
            "First call pmkisan_grievance_send_otp, then call this tool with the received OTP."
        )
    return identifier_value, reg_no_clean


def _submit_grievance_init_request(
    ctx: RunContext[FarmerContext],
    identifier_value: str,
    grievance_type: str,
    grievance_description: str,
    phone_number: str,
    raw: bool,
) -> str:
    session_id = ctx.deps.session_id
    transaction_id = generate_transaction_id(session_id, identifier_value)
    identifier_type: Literal["reg-number"] = "reg-number"

    request_obj = GrievanceInitRequest.build(
        transaction_id=transaction_id,
        identifier_value=identifier_value,
        identifier_type=identifier_type,
        grievance_type_code=GRIEVANCE_MAPPING[grievance_type],
        grievance_description=grievance_description.strip(),
        phone=str(phone_number).strip() if phone_number else "",
    )
    payload = request_obj.model_dump(by_alias=True)

    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        raise ModelRetry(_BAP_ENDPOINT_NOT_CONFIGURED_MSG)

    endpoint = f"{bap_endpoint.rstrip('/')}/init"
    logger.info(f"[PM KISAN GRIEVANCE] Request URL: {endpoint}")
    logger.info(f"[PM KISAN GRIEVANCE] Payload: {json.dumps(payload)}")

    response = httpx.post(endpoint, json=payload, timeout=get_default_httpx_timeout())

    logger.info(f"[PM KISAN GRIEVANCE] Response Status: {response.status_code}")
    logger.info(f"[PM KISAN GRIEVANCE] Response Body: {response.text[:500]}")

    if response.status_code != 200:
        logger.error(f"Grievance submission failed with status {response.status_code}")

    return _format_grievance_tool_response(response, raw)


# --------------------------------------------------------------------------------------
# Exported Tools
# --------------------------------------------------------------------------------------

def pmkisan_grievance_send_otp(
    ctx: RunContext[FarmerContext],
    reg_no: str,
    phone_number: str = "",
    purpose: Literal["submit_grievance", "check_status"] = "submit_grievance",
) -> str:
    """
    Send OTP to the mobile registered with a PM-KISAN registration number.

    Use before submitting a grievance or checking grievance status. After the user shares
    the OTP, call pmkisan_submit_grievance or pmkisan_grievance_status with that OTP.

    Args:
        reg_no: PM-KISAN registration number used for OTP.
        phone_number: Optional registered mobile number to include in the OTP payload.
        purpose: Whether the OTP is for grievance submission or status check.

    Returns:
        OTP send confirmation or service error.
    """
    try:
        if not reg_no or not reg_no.strip():
            raise ModelRetry("Please provide the PM-KISAN Registration Number to send OTP.")

        purpose_text = "submit the grievance" if purpose == "submit_grievance" else "check grievance status"
        return _request_pm_kisan_otp(ctx, str(reg_no).strip(), phone_number, purpose_text)

    except httpx.TimeoutException:
        logger.error("PM-KISAN grievance OTP request timed out.")
        return "OTP request timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"PM-KISAN grievance OTP request network error: {e}")
        return "Unable to reach OTP service. Please try again."
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"Unexpected error in pmkisan_grievance_send_otp: {e}")
        raise ModelRetry(f"Unexpected error while sending OTP. {str(e)}")


def pmkisan_submit_grievance(
    ctx: RunContext[FarmerContext],
    reg_no: str,
    grievance_description: str,
    grievance_type: GrievanceTypeLiteral,
    otp: Optional[str] = None,
    phone_number: str = "",
    raw: bool = False,
) -> str:
    """
    Submit a PM-KISAN grievance only after OTP verification.

    Call pmkisan_grievance_send_otp first. Use this tool only after the user shares
    the OTP; it verifies the OTP before calling the grievance submit API.

    Args:
        reg_no: PM-KISAN registration number used for OTP verification and grievance submission.
        grievance_description: Detailed description of the grievance.
        grievance_type: Must be exactly one of the configured grievance type codes. Map the
            farmer's free-text complaint to the closest matching value from this list; never
            invent a new label.
        otp: 4-digit OTP received on the registered mobile.
        phone_number: Optional registered mobile number to include in OTP verification.
        raw: If True, return the raw response body.

    Returns:
        Grievance submission result or OTP verification error.
    """
    try:
        identifier_value, reg_no_clean = _validate_submit_grievance_inputs(
            reg_no, grievance_type, grievance_description, otp
        )
        otp_error = _verify_pm_kisan_otp(ctx, reg_no_clean, str(otp), phone_number)
        if otp_error:
            return otp_error
        return _submit_grievance_init_request(
            ctx, identifier_value, grievance_type, grievance_description, phone_number, raw
        )

    except httpx.TimeoutException:
        logger.error("Grievance submission timed out.")
        return "Grievance submission timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Grievance submission network error: {e}")
        return "Unable to reach grievance service. Please try again."
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"Unexpected error in pmkisan_submit_grievance: {e}")
        raise ModelRetry(f"Unexpected error while submitting grievance. {str(e)}")


def pmkisan_grievance_status(
    ctx: RunContext[FarmerContext],
    reg_no: str = "",
    raw: bool = False,
    otp: Optional[str] = None,
    phone_number: str = "",
) -> str:
    """
    Check PM-KISAN grievance status only after OTP verification.

    Call pmkisan_grievance_send_otp first with purpose="check_status". Use this tool only
    after the user shares the OTP; it verifies the OTP before calling grievance search.

    Args:
        reg_no: PM-KISAN registration number used for OTP verification and status lookup.
        raw: If True, return raw HTTP body (pretty JSON when possible).
        otp: 4-digit OTP received on the registered mobile.
        phone_number: Optional registered mobile number to include in OTP verification.

    Returns:
        Grievance status summary or OTP verification error.
    """
    try:
        if not reg_no or not reg_no.strip():
            raise ModelRetry(
                "Please provide the PM-KISAN Registration Number to check grievance status."
            )

        identifier_value = str(reg_no).strip()
        identifier_type: Literal["reg-number"] = "reg-number"

        reg_no_clean = identifier_value
        if not otp or not str(otp).strip():
            raise ModelRetry(
                "OTP verification is required before checking grievance status. "
                "First call pmkisan_grievance_send_otp with purpose='check_status', then call this tool with the received OTP."
            )

        otp_error = _verify_pm_kisan_otp(ctx, reg_no_clean, str(otp), phone_number)
        if otp_error:
            return otp_error

        session_id = ctx.deps.session_id
        transaction_id = generate_transaction_id(session_id, identifier_value)

        payload = {
            "context": Context(action="search", transaction_id=transaction_id).model_dump(by_alias=True),
            "message": {
                "intent": {
                    "category": {
                        "descriptor": {
                            "name": "grievance-agri",
                            "code": "grievance",
                        }
                    },
                    "order": {
                        "fulfillments": [
                            {
                                "customer": {
                                    "person": {
                                        "name": "Customer Name",
                                        "tags": [
                                            {
                                                "display": True,
                                                "descriptor": {
                                                    "name": "Registration Details",
                                                    "code": "reg-details",
                                                },
                                                "list": [
                                                    {
                                                        "descriptor": {
                                                            "name": _REGISTRATION_NUMBER_LABEL,
                                                            "code": identifier_type,
                                                        },
                                                        "value": identifier_value,
                                                        "display": True,
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                }
                            }
                        ]
                    },
                }
            },
        }

        bap_endpoint = os.getenv("BAP_ENDPOINT")
        if not bap_endpoint:
            return "Grievance status check is currently unavailable (BAP not configured)."

        endpoint = f"{bap_endpoint.rstrip('/')}/search"
        logger.info(f"[PM KISAN GRIEVANCE STATUS] Request URL: {endpoint}")

        response = httpx.post(endpoint, json=payload, timeout=get_default_httpx_timeout())

        return _format_grievance_tool_response(response, raw)

    except httpx.TimeoutException:
        logger.error("Grievance status check timed out.")
        return "Grievance status check timed out. Please try again."
    except httpx.RequestError as e:
        logger.error(f"Grievance status network error: {e}")
        return "Unable to reach grievance status service. Please try again."
    except ModelRetry as e:
        return str(e)
    except Exception as e:
        logger.error(f"Error in pmkisan_grievance_status: {e}")
        return "An error occurred while checking grievance status. Please try again later."
