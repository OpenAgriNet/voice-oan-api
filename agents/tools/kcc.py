"""KCC (Kisan Credit Card) application status, via the Kisan Rin / Krishika portal.

Two steps: initiate_kcc_otp sends an OTP to the farmer's mobile, then
check_kcc_application_status submits that OTP, which the portal checks and answers
with the applications in the same call — there is no separate verify step. When the
mobile has several applications, select_kcc_application picks one without a new OTP.
"""

import copy
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

import httpx
from langfuse import observe
from pydantic_ai.tools import RunContext

from agents.deps import FarmerContext
from agents.tools.aif import _descriptor, _endpoint, _numeric, _unwrap, _values
from agents.tools.pmkisan_scheme_status import generate_transaction_id
from app.config import get_default_httpx_timeout
from app.observability.langfuse_client import safe_update_current_span
from helpers.utils import get_logger, to_ascii_digits

logger = get_logger(__name__)

PROVIDER_ID = "kcc-agri"
ITEM_ID = "kcc-status"
DOMAIN = "schemes:vistaar"
SOURCE_LINE = "**Source:** Kisan Rin Portal"

UNAVAILABLE = "The KCC system cannot be reached right now. Please try again in a few minutes."
NEED_MOBILE = "Ask the farmer for the 10-digit mobile number they used for their KCC application."
NEED_OTP = "Ask the farmer for the 6-digit OTP sent to their mobile."
NEED_APPLICATION_NO = "Ask the farmer which KCC application number to check (digits only)."


class _KccUnavailable(Exception):
    """Carries farmer-facing wording for a config, transport, or malformed-response failure."""


def _mobile(value: str) -> str:
    """10-digit Indian mobile with any +91 / 91 / 0 prefix removed; "" when invalid."""
    digits = re.sub(r"[\s-]", "", to_ascii_digits(str(value or "")))
    digits = re.sub(r"^(\+?91|0)(?=\d{10}$)", "", digits)
    return digits if re.fullmatch(r"[6-9]\d{9}", digits) else ""


def _build_payload(
    action: str,
    transaction_id: str,
    ctx: RunContext[FarmerContext],
    **tags: str,
) -> Dict[str, Any]:
    """Beckn envelope for /init or /status. Tags carry request_type and the KCC inputs."""
    order: Dict[str, Any] = {
        "provider": {"id": PROVIDER_ID},
        "items": [{"id": ITEM_ID}],
        "fulfillments": [
            {
                "customer": {
                    "person": {
                        "tags": [
                            {"descriptor": {"code": code}, "value": value}
                            for code, value in tags.items()
                            if value
                        ]
                    }
                }
            }
        ],
    }
    message: Dict[str, Any] = {"order": order}
    if action == "status":
        order["id"] = transaction_id
        # Required by the BAP client's Beckn schema on /status; see aif._build_payload.
        message["order_id"] = transaction_id

    return {
        "context": {
            "domain": DOMAIN,
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
            "location": {
                "country": {"code": "IND"},
                "city": {"code": "*"},
            },
            "tags": {
                "session_id": ctx.deps.session_id or "",
                "question_id": ctx.deps.question_id or "",
            },
        },
        "message": message,
    }


def _loggable(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Copy of the payload with the OTP masked — OTP values are never written to logs."""
    redacted = copy.deepcopy(payload)
    for tag in redacted["message"]["order"]["fulfillments"][0]["customer"]["person"]["tags"]:
        if tag["descriptor"]["code"] == "otp":
            tag["value"] = "****"
    return redacted


def _request(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POSTs to the BAP and returns the decoded envelope; every failure is `_KccUnavailable`."""
    try:
        endpoint = _endpoint(action)
        logger.info(f"[KCC {action.upper()}] Request URL: {endpoint}")
        logger.info(f"[KCC {action.upper()}] Request Payload: {json.dumps(_loggable(payload))}")

        response = httpx.post(endpoint, json=payload, timeout=get_default_httpx_timeout())
        logger.info(f"[KCC {action.upper()}] Response Status: {response.status_code}")
        logger.info(f"[KCC {action.upper()}] Response Payload: {response.text}")

        if not 200 <= response.status_code < 300:
            logger.error(
                "KCC %s rejected with HTTP %s: %s",
                action,
                response.status_code,
                (response.text or "")[:500],
            )
            raise _KccUnavailable(UNAVAILABLE)
        return response.json()

    except _KccUnavailable:
        raise
    except httpx.TimeoutException as e:
        logger.error(f"KCC {action} request timed out")
        raise _KccUnavailable(UNAVAILABLE) from e
    except Exception as e:
        logger.error(f"KCC {action} failed: {e}")
        raise _KccUnavailable(UNAVAILABLE) from e


def _order(envelope: Dict[str, Any]) -> Dict[str, Any]:
    return (_unwrap(envelope).get("message") or {}).get("order") or {}


def _render_otp_sent(envelope: Dict[str, Any]) -> str:
    items = _order(envelope).get("items") or []
    tags = ((items[0] or {}).get("tags") or []) if items else []
    tag = tags[0] if tags else {}
    code, short_desc = _descriptor(tag)
    values = _values(tag)

    if code != "otp_sent":
        return short_desc or UNAVAILABLE

    lines = [short_desc]
    if values.get("masked_mobile"):
        lines.append(f"Sent to mobile: {values['masked_mobile']}")
    if values.get("expires_in"):
        lines.append(f"OTP valid for: {values['expires_in']}")
    return "\n".join(lines)


def _rupees(value: str) -> str:
    """Indian digit grouping (1,00,000); passes non-numeric values through."""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return value
    whole, _, fraction = f"{amount:.2f}".partition(".")
    head, tail = whole[:-3], whole[-3:]
    if head:
        head = ",".join(re.findall(r"\d{1,2}(?=(?:\d{2})*$)", head))
        whole = f"{head},{tail}"
    return f"₹{whole}" + ("" if fraction == "00" else f".{fraction}")


APPROVED_STATUSES = {"APPROVED", "SANCTIONED", "DISBURSED"}


def _is_rejected(values: Dict[str, str]) -> bool:
    # The portal sends a rejected application back to DRAFT, so the bare status hides
    # the rejection; rejected_by is what tells the two apart.
    return bool(values.get("rejected_by")) or values.get("status", "").upper() == "REJECTED"


def _is_approved(values: Dict[str, str]) -> bool:
    return (
        values.get("status", "").upper() in APPROVED_STATUSES
        or bool(values.get("sanctioned_amount"))
    )


def _bank(values: Dict[str, str]) -> str:
    return " ".join(v for v in (values.get("bank_name"), values.get("branch_name")) if v)


def _render_application(values: Dict[str, str]) -> str:
    """One application, in the wording agreed with the KCC team for each outcome."""
    name = values.get("farmer_name") or "Farmer"
    number = values.get("application_no", "")
    status = values.get("status", "")
    amount = _rupees(values["required_loan_amount"]) if values.get("required_loan_amount") else ""
    bank = _bank(values)

    if _is_rejected(values):
        # No bank on a rejection that happened before a branch was assigned; the portal
        # still says who rejected it.
        by = bank or values.get("rejected_by") or "the bank"
        sentence = f"Hi {name}, your loan application {number}"
        if amount:
            sentence += f" for amount {amount}"
        sentence += f" was rejected by {by}"
        if values.get("rejection_reason"):
            sentence += f" due to {values['rejection_reason']}"
        sentence += "."
        if status.upper() == "DRAFT":
            sentence += (
                " It is back in the Drafts section of the Krishika app,"
                " where it can be corrected and resubmitted."
            )
    elif _is_approved(values):
        sentence = f"Hi {name}, your loan application {number} has been approved"
        if bank:
            sentence += f" by {bank}"
        sentence += "."
        if amount:
            sentence += f" Your requested amount was {amount}."
        if values.get("sanctioned_amount"):
            sentence += f" The loan has been sanctioned for {_rupees(values['sanctioned_amount'])}."
    else:
        sentence = f"Hi {name}, your loan application {number}"
        if amount:
            sentence += f" for amount {amount}"
        sentence += f" is currently {status or 'under process'}"
        if bank:
            sentence += f" with {bank}"
        sentence += "."

    lines = [SOURCE_LINE, "", sentence]
    if values.get("remark"):
        lines += ["", f"Remarks: {values['remark']}"]
    return "\n".join(lines)


def _render_application_list(order: Dict[str, Any], short_desc: str) -> str:
    applications = [
        _values((item.get("tags") or [{}])[0]) for item in order.get("items") or []
    ]
    lines = [
        SOURCE_LINE,
        "",
        f"{len(applications)} KCC applications found for this mobile number:",
    ]
    for app in applications:
        detail = ", ".join(
            v
            for v in (
                app.get("status"),
                _rupees(app["required_loan_amount"]) if app.get("required_loan_amount") else "",
            )
            if v
        )
        lines.append(f"- {app.get('application_no', '')}" + (f" ({detail})" if detail else ""))
    lines += [
        "",
        "Share these application numbers and ask the farmer which one to check. "
        "Then call select_kcc_application with that number — no new OTP is needed.",
    ]
    return "\n".join(lines)


def _render_application_status(envelope: Dict[str, Any]) -> str:
    order = _order(envelope)
    tag = (order.get("tags") or [{}])[0]
    code, short_desc = _descriptor(tag)

    if code == "application_status":
        return _render_application(_values(tag))
    if code == "multiple_applications":
        return _render_application_list(order, short_desc)
    if code == "no_applications":
        return f"{SOURCE_LINE}\n\n{short_desc}"
    return short_desc or UNAVAILABLE


@observe(name="tool:initiate_kcc_otp", as_type="tool")
def initiate_kcc_otp(ctx: RunContext[FarmerContext], mobile_number: str) -> str:
    """Send an OTP to the farmer's mobile to check their Kisan Credit Card (KCC) application status.

    First step of the KCC application status check. The OTP is valid for 15 minutes.

    Args:
        mobile_number (str): The farmer's 10-digit mobile number used for the KCC application.

    Returns:
        str: Confirmation that the OTP was sent, or the reason it failed.
    """
    mobile_number = _mobile(mobile_number)
    if not mobile_number:
        return NEED_MOBILE

    transaction_id = generate_transaction_id(ctx.deps.session_id, mobile_number)
    safe_update_current_span(
        metadata={"tool": "kcc.get_otp", "transaction_id": transaction_id}
    )

    payload = _build_payload(
        "init",
        transaction_id,
        ctx,
        request_type="get_otp",
        mobile_number=mobile_number,
    )
    try:
        return _render_otp_sent(_request("init", payload))
    except _KccUnavailable as e:
        return str(e)


@observe(name="tool:check_kcc_application_status", as_type="tool")
def check_kcc_application_status(
    ctx: RunContext[FarmerContext], mobile_number: str, otp: str
) -> str:
    """Check the farmer's Kisan Credit Card (KCC) application status. Call after initiate_kcc_otp.

    Submits the OTP; the portal verifies it and returns the application in the same call.

    Args:
        mobile_number (str): The same mobile number used for initiate_kcc_otp.
        otp (str): The 6-digit OTP the farmer received by SMS.

    Returns:
        str: The application status and details, or the reason it failed.
    """
    mobile_number = _mobile(mobile_number)
    if not mobile_number:
        return NEED_MOBILE

    otp = _numeric(otp)
    if len(otp) != 6:
        return NEED_OTP

    transaction_id = generate_transaction_id(ctx.deps.session_id, mobile_number)
    safe_update_current_span(
        metadata={"tool": "kcc.application_status", "transaction_id": transaction_id}
    )

    payload = _build_payload(
        "status",
        transaction_id,
        ctx,
        request_type="application_status",
        mobile_number=mobile_number,
        otp=otp,
    )
    try:
        return _render_application_status(_request("status", payload))
    except _KccUnavailable as e:
        return str(e)


@observe(name="tool:select_kcc_application", as_type="tool")
def select_kcc_application(
    ctx: RunContext[FarmerContext], mobile_number: str, application_no: str
) -> str:
    """Show one KCC application after check_kcc_application_status listed several.

    Uses the applications already fetched with the OTP; no new OTP is needed.

    Args:
        mobile_number (str): The same mobile number used for initiate_kcc_otp.
        application_no (str): The application number the farmer chose from the list.

    Returns:
        str: That application's status, or the reason it failed.
    """
    mobile_number = _mobile(mobile_number)
    if not mobile_number:
        return NEED_MOBILE

    application_no = _numeric(application_no)
    if not application_no:
        return NEED_APPLICATION_NO

    transaction_id = generate_transaction_id(ctx.deps.session_id, mobile_number)
    safe_update_current_span(
        metadata={"tool": "kcc.select_application", "transaction_id": transaction_id}
    )

    payload = _build_payload(
        "status",
        transaction_id,
        ctx,
        request_type="application_status",
        mobile_number=mobile_number,
        application_no=application_no,
    )
    try:
        return _render_application_status(_request("status", payload))
    except _KccUnavailable as e:
        return str(e)
