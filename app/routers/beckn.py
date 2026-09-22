"""Internal callback ingress for ONIX-validated Beckn callbacks."""

from __future__ import annotations

import hmac
import json
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse

from agents.tools.beckn.operations import get_beckn_operation_store
from app.config import settings

router = APIRouter(prefix="/beckn", tags=["beckn-internal"])

_SUPPORTED_CALLBACKS = {"on_search", "on_init", "on_confirm", "on_status"}


def _setting(name: str, default: Any) -> Any:
    return getattr(settings, name, default)


def _ack() -> dict[str, Any]:
    return {"message": {"ack": {"status": "ACK"}}}


def _nack(code: str, message: str) -> dict[str, Any]:
    return {
        "message": {"ack": {"status": "NACK"}},
        "error": {"code": code, "message": message},
    }


@router.post("/{callback_action}")
async def receive_callback(
    callback_action: str,
    payload: dict[str, Any],
    x_beckn_callback_token: str | None = Header(default=None),
) -> JSONResponse:
    """Persist a callback quickly and return a Beckn ACK/NACK.

    This route is intended to be the internal target of ONIX's BAP receiver.
    ONIX remains responsible for subscriber lookup and signature validation.
    """
    if callback_action not in _SUPPORTED_CALLBACKS:
        raise HTTPException(status_code=404, detail="Unsupported Beckn callback")

    configured_token = _setting("beckn_callback_token", None)
    context = payload.get("context") or {}
    is_tokenless_vistaar_shc = (
        not configured_token
        and _setting("vistaar_shc_enabled", False)
        and callback_action == "on_init"
        and context.get("action") == "on_init"
        and context.get("domain") == "schemes:vistaar"
    )
    if not configured_token and not is_tokenless_vistaar_shc:
        return JSONResponse(
            status_code=503,
            content=_nack("CALLBACK_AUTH_NOT_CONFIGURED", "Callback ingress authentication is not configured"),
        )
    if configured_token and not hmac.compare_digest(x_beckn_callback_token or "", configured_token):
        return JSONResponse(
            status_code=401,
            content=_nack("UNAUTHORIZED_CALLBACK", "Invalid callback ingress token"),
        )

    max_body_bytes = int(_setting("beckn_callback_max_body_bytes", 2 * 1024 * 1024))
    encoded_size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    if encoded_size > max_body_bytes:
        return JSONResponse(
            status_code=413,
            content=_nack("CALLBACK_TOO_LARGE", "Callback exceeds configured size limit"),
        )
    if (payload.get("context") or {}).get("action") != callback_action:
        return JSONResponse(
            status_code=200,
            content=_nack("ACTION_MISMATCH", "URL action does not match context.action"),
        )

    result = await get_beckn_operation_store().record_callback(payload)
    if result.accepted:
        return JSONResponse(status_code=200, content=_ack())
    return JSONResponse(
        status_code=200,
        content=_nack(result.code or "CALLBACK_REJECTED", result.message or "Callback rejected"),
    )

