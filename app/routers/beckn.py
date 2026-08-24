"""Internal callback ingress and owner-scoped operation status."""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Body, Depends, Header, HTTPException, status
from fastapi.responses import JSONResponse

from app.auth.jwt_auth import get_current_user
from app.config import settings
from app.services.beckn_transactions import (
    CallbackCorrelationError,
    common_ack,
    common_nack,
    get_beckn_facade,
)
from app.services.voice_identity import resolve_caller_identity

router = APIRouter(prefix="/beckn", tags=["beckn"])
_CALLBACK_ACTIONS = {"on_search", "on_select", "on_init", "on_confirm", "on_status"}


def _authorize_callback(x_beckn_callback_token: str | None = Header(None)) -> None:
    expected = settings.beckn_callback_token or ""
    supplied = x_beckn_callback_token or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Beckn callback ingress is not configured",
        )
    if not hmac.compare_digest(expected, supplied):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid callback token")


@router.post("/callbacks/{callback_action}", dependencies=[Depends(_authorize_callback)])
async def receive_callback(
    callback_action: str,
    payload: dict = Body(...),
):
    if callback_action not in _CALLBACK_ACTIONS:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=common_nack("UNSUPPORTED_CALLBACK", "Unsupported Beckn callback action"),
        )
    try:
        await get_beckn_facade().accept_callback(callback_action, payload)
    except CallbackCorrelationError as exc:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=common_nack("CALLBACK_CORRELATION_FAILED", str(exc)),
        )
    return common_ack()


@router.get("/operations/{transaction_id}")
async def get_operation_status(
    transaction_id: str,
    user_info: dict = Depends(get_current_user),
):
    identity = resolve_caller_identity(user_info, "anonymous")
    operation = await get_beckn_facade().get_for_subject(transaction_id, identity.subject_id)
    if operation is None:
        # Do not reveal whether another caller owns the transaction.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Operation not found")
    return {
        "operation_id": operation.operation_id,
        "transaction_id": operation.transaction_id,
        "tool_name": operation.tool_name,
        "state": operation.state,
        "provider_order_id": operation.provider_order_id,
        "error": operation.error_payload,
        "created_at": operation.created_at,
        "updated_at": operation.updated_at,
    }

