"""Durable, callback-aware Beckn transaction façade for voice tools.

The façade deliberately separates a conversational session from a Beckn
transaction. It stores correlation state before sending, treats an ACK only as
acceptance, retains late callbacks, and never blindly resubmits a side effect
after an ambiguous timeout.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import settings
from app.core.cache import build_cache_key, redis_client
from helpers.utils import get_logger

logger = get_logger(__name__)


class OperationState(str, Enum):
    CREATED = "CREATED"
    SENT = "SENT"
    ACKED_WAITING_CALLBACK = "ACKED_WAITING_CALLBACK"
    SUCCEEDED = "SUCCEEDED"
    BUSINESS_FAILED = "BUSINESS_FAILED"
    NACKED = "NACKED"
    TIMED_OUT_PENDING = "TIMED_OUT_PENDING"
    UNKNOWN_REQUIRES_RECONCILIATION = "UNKNOWN_REQUIRES_RECONCILIATION"


TERMINAL_STATES = {
    OperationState.SUCCEEDED,
    OperationState.BUSINESS_FAILED,
    OperationState.NACKED,
}
PENDING_STATES = {
    OperationState.CREATED,
    OperationState.SENT,
    OperationState.ACKED_WAITING_CALLBACK,
    OperationState.TIMED_OUT_PENDING,
    OperationState.UNKNOWN_REQUIRES_RECONCILIATION,
}


class BecknOperation(BaseModel):
    operation_id: str
    session_id: str
    subject_id: str
    tool_name: str
    domain: str
    action: str
    transaction_id: str
    business_key_hash: str
    request_hash: str
    side_effecting: bool = False
    bpp_id: str
    bpp_uri: str
    expected_callbacks: dict[str, str] = Field(default_factory=dict)
    callback_hashes: list[str] = Field(default_factory=list)
    state: OperationState = OperationState.CREATED
    response_payload: dict[str, Any] | None = None
    error_payload: dict[str, Any] | None = None
    provider_order_id: str | None = None
    created_at: str
    updated_at: str
    deadline_at: str


class BecknResult(BaseModel):
    state: OperationState
    transaction_id: str
    operation_id: str
    payload: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    provider_order_id: str | None = None

    @property
    def completed(self) -> bool:
        return self.state == OperationState.SUCCEEDED

    @property
    def pending(self) -> bool:
        return self.state in PENDING_STATES


class CallbackCorrelationError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utcnow()).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _operation_key(transaction_id: str) -> str:
    return build_cache_key(f"beckn:operation:{transaction_id}")


def _business_key(key_hash: str) -> str:
    return build_cache_key(f"beckn:business:{key_hash}")


class RedisOperationStore:
    """Redis persistence with atomic business-idempotency reservation."""

    async def create_or_get(self, operation: BecknOperation) -> tuple[BecknOperation, bool]:
        operation_json = operation.model_dump_json()
        script = """
        local existing = redis.call('get', KEYS[1])
        if existing then
          return existing
        end
        redis.call('set', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[3]))
        redis.call('set', KEYS[2], ARGV[2], 'EX', tonumber(ARGV[3]))
        return ARGV[1]
        """
        transaction_id = await redis_client.eval(
            script,
            2,
            _business_key(operation.business_key_hash),
            _operation_key(operation.transaction_id),
            operation.transaction_id,
            operation_json,
            str(settings.beckn_operation_ttl_seconds),
        )
        created = str(transaction_id) == operation.transaction_id
        if created:
            return operation, True
        existing = await self.get(str(transaction_id))
        if existing is None:
            # A legacy/expired dangling index must fail closed; silently creating
            # a second booking would defeat the business idempotency key.
            raise RuntimeError("Beckn idempotency index points to a missing operation")
        return existing, False

    async def get(self, transaction_id: str) -> BecknOperation | None:
        raw = await redis_client.get(_operation_key(transaction_id))
        return BecknOperation.model_validate_json(raw) if raw else None

    async def save(self, operation: BecknOperation) -> None:
        operation.updated_at = _timestamp()
        await redis_client.set(
            _operation_key(operation.transaction_id),
            operation.model_dump_json(),
            ex=settings.beckn_operation_ttl_seconds,
        )
        await redis_client.expire(
            _business_key(operation.business_key_hash),
            settings.beckn_operation_ttl_seconds,
        )


class BecknTransactionFacade:
    def __init__(self, store: RedisOperationStore | None = None):
        self.store = store or RedisOperationStore()

    def _require_config(self) -> None:
        if not settings.beckn_transaction_bridge_url:
            raise RuntimeError("BECKN_TRANSACTION_BRIDGE_URL is required when VOICE_BECKN_ENABLED=true")
        if not settings.beckn_transaction_bridge_token:
            raise RuntimeError("BECKN_TRANSACTION_BRIDGE_TOKEN is required when VOICE_BECKN_ENABLED=true")

    def _context(
        self,
        *,
        domain: str,
        action: str,
        transaction_id: str,
        message_id: str,
        bpp_id: str,
        bpp_uri: str,
    ) -> dict[str, Any]:
        return {
            "domain": domain,
            "location": {
                "country": {"code": settings.beckn_location_country},
                "city": {"code": settings.beckn_location_city},
            },
            "action": action,
            "version": "1.1.0",
            "bap_id": settings.beckn_bap_id,
            "bap_uri": settings.beckn_bap_uri,
            "bpp_id": bpp_id,
            "bpp_uri": bpp_uri,
            "transaction_id": transaction_id,
            "message_id": message_id,
            "timestamp": _timestamp(),
            "ttl": "PT30S",
        }

    async def execute(
        self,
        *,
        session_id: str,
        subject_id: str,
        tool_name: str,
        domain: str,
        action: str,
        message: dict[str, Any],
        business_key: str,
        side_effecting: bool = False,
        expected_callback: str | None = None,
        bpp_id: str | None = None,
        bpp_uri: str | None = None,
    ) -> BecknResult:
        """Create/reuse an operation, send once, then await its paired callback."""
        self._require_config()
        callback_action = expected_callback or f"on_{action}"
        selected_bpp_id = bpp_id or settings.beckn_bpp_id
        selected_bpp_uri = bpp_uri or settings.beckn_bpp_uri
        transaction_id = str(uuid.uuid4())
        message_id = str(uuid.uuid4())
        business_key_hash = _canonical_hash({"subject": subject_id, "key": business_key})
        # Work on a detached JSON value so adding the provider-visible stable key
        # cannot mutate a caller-owned object reused by the agent.
        message = json.loads(json.dumps(message, ensure_ascii=False))
        if side_effecting and isinstance(message.get("order"), dict):
            message["order"].setdefault("tags", []).append({
                "descriptor": {"code": "client-reference"},
                "list": [{
                    "descriptor": {"code": "idempotency_key"},
                    "value": business_key_hash,
                }],
            })
        context = self._context(
            domain=domain,
            action=action,
            transaction_id=transaction_id,
            message_id=message_id,
            bpp_id=selected_bpp_id,
            bpp_uri=selected_bpp_uri,
        )
        envelope = {"context": context, "message": message}
        now = _utcnow()
        operation = BecknOperation(
            operation_id=str(uuid.uuid4()),
            session_id=session_id,
            subject_id=subject_id,
            tool_name=tool_name,
            domain=domain,
            action=action,
            transaction_id=transaction_id,
            business_key_hash=business_key_hash,
            request_hash=_canonical_hash(envelope),
            side_effecting=side_effecting,
            bpp_id=selected_bpp_id,
            bpp_uri=selected_bpp_uri,
            expected_callbacks={callback_action: message_id},
            created_at=_timestamp(now),
            updated_at=_timestamp(now),
            deadline_at=_timestamp(now + timedelta(seconds=settings.beckn_callback_wait_seconds)),
        )

        operation, created = await self.store.create_or_get(operation)
        if not created:
            if operation.state in TERMINAL_STATES:
                return self._result(operation)
            if operation.side_effecting and operation.state in {
                OperationState.TIMED_OUT_PENDING,
                OperationState.UNKNOWN_REQUIRES_RECONCILIATION,
            }:
                operation = await self._recover_status(operation)
            return await self._wait(operation.transaction_id)

        operation.state = OperationState.SENT
        await self.store.save(operation)
        operation = await self._post_forward(operation, action, envelope)
        if operation.state in TERMINAL_STATES:
            return self._result(operation)
        return await self._wait(operation.transaction_id)

    async def _post_forward(
        self,
        operation: BecknOperation,
        action: str,
        envelope: dict[str, Any],
    ) -> BecknOperation:
        url = f"{settings.beckn_transaction_bridge_url.rstrip('/')}/transactions/{action}"
        headers = {
            "Authorization": f"Bearer {settings.beckn_transaction_bridge_token}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=settings.beckn_http_timeout_seconds) as client:
                response = await client.post(url, json=envelope, headers=headers)
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            latest = await self.store.get(operation.transaction_id) or operation
            if latest.state in TERMINAL_STATES:
                return latest
            latest.state = (
                OperationState.UNKNOWN_REQUIRES_RECONCILIATION
                if latest.side_effecting
                else OperationState.TIMED_OUT_PENDING
            )
            latest.error_payload = {
                "code": "FORWARD_RESULT_AMBIGUOUS",
                "message": type(exc).__name__,
            }
            await self.store.save(latest)
            return latest

        latest = await self.store.get(operation.transaction_id) or operation
        if latest.state in TERMINAL_STATES:
            return latest  # fast callback won the race with the HTTP ACK
        ack_status = str((((body or {}).get("message") or {}).get("ack") or {}).get("status") or "").upper()
        if ack_status != "ACK":
            latest.state = OperationState.NACKED
            latest.error_payload = (body or {}).get("error") or {
                "code": "FORWARD_NACK",
                "message": "Beckn participant rejected the request",
            }
        else:
            latest.state = OperationState.ACKED_WAITING_CALLBACK
            latest.error_payload = None
        await self.store.save(latest)
        return latest

    async def _wait(self, transaction_id: str) -> BecknResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + settings.beckn_callback_wait_seconds
        latest: BecknOperation | None = None
        while loop.time() < deadline:
            latest = await self.store.get(transaction_id)
            if latest is None:
                raise RuntimeError("Beckn operation disappeared while awaiting callback")
            if latest.state in TERMINAL_STATES:
                return self._result(latest)
            await asyncio.sleep(settings.beckn_poll_interval_seconds)

        latest = await self.store.get(transaction_id)
        if latest is None:
            raise RuntimeError("Beckn operation disappeared after callback deadline")
        if latest.state not in TERMINAL_STATES:
            latest.state = OperationState.TIMED_OUT_PENDING
            await self.store.save(latest)
        return self._result(latest)

    async def _recover_status(self, operation: BecknOperation) -> BecknOperation:
        """Issue one directed status request for an ambiguous side effect."""
        if "on_status" in operation.expected_callbacks:
            return operation
        message_id = str(uuid.uuid4())
        operation.expected_callbacks["on_status"] = message_id
        await self.store.save(operation)  # waiter exists before forward send
        envelope = {
            "context": self._context(
                domain=operation.domain,
                action="status",
                transaction_id=operation.transaction_id,
                message_id=message_id,
                bpp_id=operation.bpp_id,
                bpp_uri=operation.bpp_uri,
            ),
            "message": {
                "order_id": operation.provider_order_id or operation.business_key_hash,
            },
        }
        return await self._post_forward(operation, "status", envelope)

    async def accept_callback(self, callback_action: str, payload: dict[str, Any]) -> BecknOperation:
        context = payload.get("context") or {}
        transaction_id = str(context.get("transaction_id") or "")
        message_id = str(context.get("message_id") or "")
        if not transaction_id or not message_id:
            raise CallbackCorrelationError("callback context requires transaction_id and message_id")

        operation = await self.store.get(transaction_id)
        if operation is None:
            raise CallbackCorrelationError("unknown or expired transaction_id")
        expected_message_id = operation.expected_callbacks.get(callback_action)
        if not expected_message_id or expected_message_id != message_id:
            raise CallbackCorrelationError("callback action/message_id does not match the operation")
        if context.get("domain") != operation.domain:
            raise CallbackCorrelationError("callback domain does not match the operation")
        if context.get("bpp_id") != operation.bpp_id:
            raise CallbackCorrelationError("callback bpp_id does not match the selected provider")

        callback_hash = _canonical_hash(payload)
        if callback_hash in operation.callback_hashes:
            return operation
        operation.callback_hashes.append(callback_hash)

        error = payload.get("error")
        if error:
            operation.state = OperationState.BUSINESS_FAILED
            operation.error_payload = error
            operation.response_payload = None
        else:
            operation.state = OperationState.SUCCEEDED
            operation.response_payload = payload
            operation.error_payload = None
            order = ((payload.get("message") or {}).get("order") or {})
            if order.get("id"):
                operation.provider_order_id = str(order["id"])
        await self.store.save(operation)
        return operation

    async def get_for_subject(self, transaction_id: str, subject_id: str) -> BecknOperation | None:
        operation = await self.store.get(transaction_id)
        if operation is None or not operation.subject_id == subject_id:
            return None
        return operation

    @staticmethod
    def _result(operation: BecknOperation) -> BecknResult:
        return BecknResult(
            state=operation.state,
            transaction_id=operation.transaction_id,
            operation_id=operation.operation_id,
            payload=operation.response_payload,
            error=operation.error_payload,
            provider_order_id=operation.provider_order_id,
        )


_facade = BecknTransactionFacade()


def get_beckn_facade() -> BecknTransactionFacade:
    return _facade


def common_ack() -> dict[str, Any]:
    return {"message": {"ack": {"status": "ACK"}}}


def common_nack(code: str, message: str) -> dict[str, Any]:
    return {
        "message": {"ack": {"status": "NACK"}},
        "error": {"type": "DOMAIN-ERROR", "code": code, "message": message},
    }
