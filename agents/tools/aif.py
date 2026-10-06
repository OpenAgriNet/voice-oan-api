"""AIF (Agriculture Infrastructure Fund) loan and grievance status.
"""

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

import httpx
from pydantic_ai.tools import RunContext

from agents.deps import FarmerContext
from agents.tools.pmkisan_scheme_status import generate_transaction_id
from app.config import get_default_httpx_timeout
from helpers.utils import get_logger, to_ascii_digits

logger = get_logger(__name__)

PROVIDER_ID = "aif-agri"
ITEM_ID = "aif-status"
DOMAIN = "schemes:vistaar"
SOURCE_LINE = "**Source:** AIF Portal"

UNAVAILABLE = "The AIF system cannot be reached right now. Please try again in a few minutes."
NEED_BENEFICIARY_ID = "Ask the farmer for their AIF beneficiary ID (digits only)."
NEED_OTP = "Ask the farmer for the 6-digit OTP sent to their registered mobile."
NEED_LOAN_NUMBER = "Ask the farmer for their AIF loan application number (digits only)."


class _AifUnavailable(Exception):
    """Carries farmer-facing wording for a config, transport, or malformed-response failure."""


def _endpoint(action: str) -> str:
    base = (os.getenv("BAP_ENDPOINT") or "").strip().rstrip("/")
    if not base:
        raise ValueError("BAP_ENDPOINT is not configured")
    return f"{base}/{action}"


def _numeric(value: str) -> str:
    """Normalises regional-script digits; returns "" when the value is not a plain run of digits."""
    cleaned = to_ascii_digits(str(value or "")).strip().replace(" ", "")
    return cleaned if re.fullmatch(r"\d+", cleaned) else ""


def _build_payload(
    action: str,
    transaction_id: str,
    ctx: RunContext[FarmerContext],
    **tags: str,
) -> Dict[str, Any]:
    """Beckn envelope for /init or /status. Tags carry request_type and the AIF inputs."""
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
        # The Beckn schema requires message.order_id on /status, and the BAP client
        # NACKs the request before it reaches the BPP without it. The BPP reads the
        # AIF inputs from message.order.fulfillments; this is for validation only.
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


def _request(action: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """POSTs to the BAP and returns the decoded envelope.

    Every failure — unconfigured endpoint, transport error, non-200, unparseable body —
    is raised as `_AifUnavailable` carrying wording that can be shown to the farmer.
    """
    try:
        endpoint = _endpoint(action)
        logger.info(f"[AIF {action.upper()}] Request URL: {endpoint}")

        response = httpx.post(endpoint, json=payload, timeout=get_default_httpx_timeout())
        logger.info(f"[AIF {action.upper()}] Response Status: {response.status_code}")

        # The network BAP answers 200; a BPP called directly answers 201 for a POST.
        if not 200 <= response.status_code < 300:
            # The farmer only ever sees the generic message, so name the real cause here:
            # a 4xx is our own malformed request (e.g. a schema NACK from the BAP client),
            # not the AIF service being down.
            logger.error(
                "AIF %s rejected with HTTP %s: %s",
                action,
                response.status_code,
                (response.text or "")[:500],
            )
            raise _AifUnavailable(UNAVAILABLE)
        return response.json()

    except _AifUnavailable:
        raise
    except httpx.TimeoutException as e:
        logger.error(f"AIF {action} request timed out")
        raise _AifUnavailable(UNAVAILABLE) from e
    except Exception as e:
        # Covers the unconfigured endpoint, a non-JSON body, and transport errors alike.
        logger.error(f"AIF {action} failed: {e}")
        raise _AifUnavailable(UNAVAILABLE) from e


def _unwrap(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Returns the BPP reply.

    The network BAP wraps replies in `responses[]`, the same as `/search`; a BPP
    called directly answers with the bare envelope. Both shapes reach this tool.
    """
    responses = envelope.get("responses")
    if isinstance(responses, list) and responses:
        first = responses[0]
        return first if isinstance(first, dict) else {}
    return envelope


def _outcome_tag(envelope: Dict[str, Any], action: str) -> Dict[str, Any]:
    """The single outcome tag: on items[0] for on_init, on the order itself for on_status."""
    order = (_unwrap(envelope).get("message") or {}).get("order") or {}
    if action == "init":
        items = order.get("items") or []
        tags = ((items[0] or {}).get("tags") or []) if items else []
    else:
        tags = order.get("tags") or []
    return tags[0] if tags else {}


def _descriptor(tag: Dict[str, Any]) -> Tuple[str, str]:
    descriptor = tag.get("descriptor") or {}
    return descriptor.get("code") or "", descriptor.get("short_desc") or ""


def _values(tag: Dict[str, Any]) -> Dict[str, str]:
    return {
        (entry.get("descriptor") or {}).get("code", ""): str(entry.get("value", ""))
        for entry in tag.get("list") or []
    }


def _shorten(text: str, limit: int = 120) -> str:
    """Collapses whitespace and caps a ticket description; farmers write long run-ons."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip(" ,.;") + "…"


def _failure(short_desc: str, values: Dict[str, str]) -> str:
    lines = [short_desc or UNAVAILABLE]
    if values.get("retryable") == "false":
        lines.append("Retry cannot succeed. Do not offer to try again.")
    return "\n".join(lines)


def _render_init(envelope: Dict[str, Any]) -> str:
    tag = _outcome_tag(envelope, "init")
    code, short_desc = _descriptor(tag)
    values = _values(tag)

    if code not in ("otp_sent", "otp_verified"):
        return _failure(short_desc, values)

    lines = [short_desc]
    if values.get("masked_mobile"):
        lines.append(f"Registered mobile: {values['masked_mobile']}")
    if values.get("beneficiary_name"):
        lines.append(f"Beneficiary: {values['beneficiary_name']}")
    return "\n".join(lines)


def _render_loan_status(envelope: Dict[str, Any]) -> str:
    tag = _outcome_tag(envelope, "status")
    code, short_desc = _descriptor(tag)
    values = _values(tag)

    if code != "loan_status":
        return _failure(short_desc, values)

    # short_desc carries only the bare status, so name the application explicitly —
    # a beneficiary can hold several, and the agent must not attach a status to the
    # wrong one.
    status = values.get("status") or short_desc
    number = values.get("loan_application_number")
    if number:
        return f"{SOURCE_LINE}\n\nLoan application {number}: {status}"
    return f"{SOURCE_LINE}\n\n{status}"


def _group_tickets(order: Dict[str, Any]) -> Dict[str, List[Dict[str, str]]]:
    """Tickets keyed by application number, in first-seen order.

    A farmer tracks their applications, and one application can carry several
    tickets. AIF reports 0 for tickets not tied to an application (the network omits
    those), so those collect under "".
    """
    grouped: Dict[str, List[Dict[str, str]]] = {}
    for item in order.get("items") or []:
        ticket = _values((item.get("tags") or [{}])[0])
        grouped.setdefault(ticket.get("loan_application_number") or "", []).append(ticket)
    return grouped


def _group_lines(key: str, tickets: List[Dict[str, str]]) -> List[str]:
    """One application's block: a header, then each ticket with its shortened description."""
    header = f"Application {key}" if key else "Not linked to an application"
    lines = [f"{header} — {len(tickets)} ticket(s)"]
    for ticket in tickets:
        kind = ticket.get("query_type") or "Support ticket"
        status = ticket.get("status") or "Unknown"
        lines.append(f"  {kind} ({status})")
        description = _shorten(ticket.get("description") or "")
        if description:
            lines.append(f"    {description}")
    lines.append("")
    return lines


def _render_grievance_status(envelope: Dict[str, Any]) -> str:
    tag = _outcome_tag(envelope, "status")
    code, short_desc = _descriptor(tag)

    # No tickets is a successful result, not an error.
    if code == "no_grievances":
        return f"{SOURCE_LINE}\n\nThere are no open grievances or support tickets."
    if code != "grievance_status":
        return _failure(short_desc, _values(tag))

    order = (_unwrap(envelope).get("message") or {}).get("order") or {}
    grouped = _group_tickets(order)
    numbered = [key for key in grouped if key]

    total = sum(len(tickets) for tickets in grouped.values())
    summary = f"{total} support ticket(s)"
    if numbered:
        summary += f" across {len(numbered)} application(s)"

    lines = [SOURCE_LINE, "", f"{summary}.", ""]
    for key in numbered + ([""] if "" in grouped else []):
        lines.extend(_group_lines(key, grouped[key]))

    return "\n".join(lines).strip()


def initiate_aif_otp(ctx: RunContext[FarmerContext], beneficiary_id: str) -> str:
    """Send an OTP to the AIF beneficiary's registered mobile.

    First step for any Agriculture Infrastructure Fund (AIF) loan or grievance status
    check. The response carries the masked mobile number the OTP was sent to.

    Args:
        beneficiary_id (str): AIF beneficiary ID, digits only (e.g. "106545").

    Returns:
        str: Confirmation with the masked mobile number, or the reason it failed.
    """
    beneficiary_id = _numeric(beneficiary_id)
    if not beneficiary_id:
        return NEED_BENEFICIARY_ID

    transaction_id = generate_transaction_id(ctx.deps.session_id, beneficiary_id)

    payload = _build_payload(
        "init",
        transaction_id,
        ctx,
        request_type="get_otp",
        beneficiary_id=beneficiary_id,
    )
    try:
        return _render_init(_request("init", payload))
    except _AifUnavailable as e:
        return str(e)


def verify_aif_otp(ctx: RunContext[FarmerContext], otp: str, beneficiary_id: str) -> str:
    """Verify the AIF OTP. Call after initiate_aif_otp, before any AIF status check.

    One verification covers both check_aif_loan_status and check_aif_grievance_status
    for the rest of the conversation, so the OTP is submitted exactly once.

    Args:
        otp (str): The 6-digit OTP the farmer received by SMS.
        beneficiary_id (str): The same AIF beneficiary ID used for initiate_aif_otp.

    Returns:
        str: Verification result, or the reason it failed.
    """
    beneficiary_id = _numeric(beneficiary_id)
    if not beneficiary_id:
        return NEED_BENEFICIARY_ID

    # AIF rejects anything that is not exactly 6 digits; catch it before the round trip.
    otp = _numeric(otp)
    if len(otp) != 6:
        return NEED_OTP

    transaction_id = generate_transaction_id(ctx.deps.session_id, beneficiary_id)

    payload = _build_payload(
        "init",
        transaction_id,
        ctx,
        request_type="verify_otp",
        beneficiary_id=beneficiary_id,
        otp=otp,
    )
    try:
        return _render_init(_request("init", payload))
    except _AifUnavailable as e:
        return str(e)


def check_aif_loan_status(
    ctx: RunContext[FarmerContext], beneficiary_id: str, loan_application_number: str
) -> str:
    """Check the status of an AIF loan application. Requires a verified OTP.

    Args:
        beneficiary_id (str): The same AIF beneficiary ID used for verify_aif_otp.
        loan_application_number (str): AIF loan application number, digits only. Lengths vary.

    Returns:
        str: The loan application status, or the reason it failed.
    """
    beneficiary_id = _numeric(beneficiary_id)
    if not beneficiary_id:
        return NEED_BENEFICIARY_ID

    loan_application_number = _numeric(loan_application_number)
    if not loan_application_number:
        return NEED_LOAN_NUMBER

    transaction_id = generate_transaction_id(ctx.deps.session_id, beneficiary_id)

    payload = _build_payload(
        "status",
        transaction_id,
        ctx,
        request_type="loan_status",
        beneficiary_id=beneficiary_id,
        loan_application_number=loan_application_number,
    )
    try:
        return _render_loan_status(_request("status", payload))
    except _AifUnavailable as e:
        return str(e)


def check_aif_grievance_status(ctx: RunContext[FarmerContext], beneficiary_id: str) -> str:
    """Check AIF support tickets / grievances for a beneficiary. Requires a verified OTP.

    Args:
        beneficiary_id (str): The same AIF beneficiary ID used for verify_aif_otp.

    Returns:
        str: The support tickets and their statuses, or the reason it failed.
    """
    beneficiary_id = _numeric(beneficiary_id)
    if not beneficiary_id:
        return NEED_BENEFICIARY_ID

    transaction_id = generate_transaction_id(ctx.deps.session_id, beneficiary_id)

    payload = _build_payload(
        "status",
        transaction_id,
        ctx,
        request_type="grievance_status",
        beneficiary_id=beneficiary_id,
    )
    try:
        return _render_grievance_status(_request("status", payload))
    except _AifUnavailable as e:
        return str(e)
