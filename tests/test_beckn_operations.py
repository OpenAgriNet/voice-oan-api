"""Beckn operation-store and callback-ingress invariants used by voice.

These tests lock the durable async contract: idempotent creates, terminal
callback precedence over ACK races, conflict rejection, timeout recovery, and
ingress auth that refuses unauthenticated callbacks.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from agents.tools.beckn.operations import (
    BecknOperationStore,
    CallbackRecordResult,
    OperationState,
    validate_beckn_startup_configuration,
)


class MemoryRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.hashes: dict[str, dict[str, str]] = {}

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, key):
        self.values.pop(key, None)
        self.hashes.pop(key, None)

    async def hset(self, key, mapping=None):
        self.hashes.setdefault(key, {}).update({k: str(v) for k, v in (mapping or {}).items()})
        return len(mapping or {})

    async def hsetnx(self, key, field, value):
        row = self.hashes.setdefault(key, {})
        if field in row:
            return 0
        row[field] = value
        return 1

    async def hget(self, key, field):
        return self.hashes.get(key, {}).get(field)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def expire(self, key, ttl):
        return True


def _create(store: BecknOperationStore, *, idem="tool-call-1"):
    return store.create(
        operation_id="op-1",
        transaction_id="11111111-1111-4111-8111-111111111111",
        message_id="22222222-2222-4222-8222-222222222222",
        action="confirm",
        expected_callback="on_confirm",
        domain="services:amul-vet-booking",
        bap_id="bap.amul-net.internal",
        bpp_id="bpp-amul.amul-net.internal",
        request_payload={"context": {"action": "confirm"}, "message": {}},
        idempotency_key=idem,
        session_id="session-1",
        tool_call_id="tool-call-1",
    )


def _callback(ticket="T-1") -> dict[str, Any]:
    return {
        "context": {
            "domain": "services:amul-vet-booking",
            "action": "on_confirm",
            "version": "1.1.0",
            "bap_id": "bap.amul-net.internal",
            "bpp_id": "bpp-amul.amul-net.internal",
            "transaction_id": "11111111-1111-4111-8111-111111111111",
            "message_id": "22222222-2222-4222-8222-222222222222",
        },
        "message": {"order": {"id": ticket, "status": "ACTIVE"}},
    }


def test_idempotent_create_reuses_existing_operation():
    async def _run():
        store = BecknOperationStore(MemoryRedis(), ttl_seconds=3600)
        first = await _create(store)
        second = await _create(store)
        assert first.created is True
        assert second.created is False
        assert second.operation.transaction_id == first.operation.transaction_id
        assert second.operation.message_id == first.operation.message_id

    asyncio.run(_run())


def test_callback_before_ack_stays_succeeded_and_duplicates_are_idempotent():
    """A callback that races the HTTP ACK must still win; redelivery must not rewrite it."""

    async def _run():
        store = BecknOperationStore(MemoryRedis(), ttl_seconds=3600)
        created = await _create(store)

        accepted = await store.record_callback(_callback())
        await store.mark_ack(created.operation, {"message": {"ack": {"status": "ACK"}}})
        duplicate = await store.record_callback(_callback())

        operation = await store.get(created.operation.transaction_id, created.operation.message_id)
        assert accepted.accepted and not accepted.duplicate
        assert duplicate.accepted and duplicate.duplicate
        assert operation is not None
        assert operation.state is OperationState.SUCCEEDED
        assert operation.callback["message"]["order"]["id"] == "T-1"

    asyncio.run(_run())


def test_conflicting_second_callback_is_rejected_without_overwrite():
    async def _run():
        store = BecknOperationStore(MemoryRedis(), ttl_seconds=3600)
        created = await _create(store)
        assert (await store.record_callback(_callback("T-1"))).accepted

        conflict = await store.record_callback(_callback("T-2"))
        operation = await store.get(created.operation.transaction_id, created.operation.message_id)

        assert not conflict.accepted
        assert conflict.code == "CALLBACK_CONFLICT"
        assert operation.callback["message"]["order"]["id"] == "T-1"

    asyncio.run(_run())


def test_timeout_stays_pending_until_late_callback_arrives():
    async def _run():
        store = BecknOperationStore(MemoryRedis(), ttl_seconds=3600)
        created = await _create(store)
        await store.mark_ack(created.operation, {"message": {"ack": {"status": "ACK"}}})

        timed_out = await store.wait(created.operation, timeout_seconds=0)
        assert timed_out.state is OperationState.TIMED_OUT_PENDING

        assert (await store.record_callback(_callback())).accepted
        recovered = await store.get(created.operation.transaction_id, created.operation.message_id)
        assert recovered.state is OperationState.SUCCEEDED

    asyncio.run(_run())


def test_callback_rejects_participant_mismatch():
    async def _run():
        store = BecknOperationStore(MemoryRedis(), ttl_seconds=3600)
        await _create(store)
        payload = _callback()
        payload["context"]["bpp_id"] = "attacker.example"

        result = await store.record_callback(payload)
        assert not result.accepted
        assert result.code == "CORRELATION_MISMATCH"

    asyncio.run(_run())


def test_startup_validation_requires_callback_token_and_bridge_token(monkeypatch):
    from agents.tools.beckn import operations as module

    monkeypatch.setattr(module.settings, "beckn_bap_caller_url", "http://onix/bap/caller")
    monkeypatch.setattr(module.settings, "beckn_transaction_bridge_token", "transaction-secret")
    monkeypatch.setattr(module.settings, "beckn_bap_id", "bap.amul-net.internal")
    monkeypatch.setattr(module.settings, "beckn_bap_uri", "https://bap.example/bap/receiver")
    monkeypatch.setattr(module.settings, "beckn_amul_bpp_id", "bpp.example")
    monkeypatch.setattr(module.settings, "beckn_amul_bpp_uri", "https://bpp.example/bpp/receiver")
    monkeypatch.setattr(module.settings, "beckn_booking_domain", "services:amul-vet-booking")
    monkeypatch.setattr(module.settings, "beckn_callback_token", "callback-secret")

    validate_beckn_startup_configuration()

    monkeypatch.setattr(module.settings, "beckn_callback_token", None)
    with pytest.raises(RuntimeError, match="BECKN_CALLBACK_TOKEN"):
        validate_beckn_startup_configuration()


def test_callback_ingress_acks_only_after_store_accepts(monkeypatch):
    from app.routers import beckn as router_module

    calls = {"n": 0}

    class Store:
        async def record_callback(self, payload):
            calls["n"] += 1
            return CallbackRecordResult(True)

    monkeypatch.setattr(router_module.settings, "beckn_callback_token", "secret")
    monkeypatch.setattr(router_module, "get_beckn_operation_store", lambda: Store())

    accepted = asyncio.run(router_module.receive_callback("on_confirm", _callback(), "secret"))
    assert accepted.status_code == 200
    assert json.loads(accepted.body)["message"]["ack"]["status"] == "ACK"
    assert calls["n"] == 1


def test_callback_ingress_rejects_missing_token_without_touching_store(monkeypatch):
    from app.routers import beckn as router_module

    calls = {"n": 0}

    class Store:
        async def record_callback(self, payload):
            calls["n"] += 1
            return CallbackRecordResult(True)

    monkeypatch.setattr(router_module.settings, "beckn_callback_token", "secret")
    monkeypatch.setattr(router_module, "get_beckn_operation_store", lambda: Store())

    rejected = asyncio.run(router_module.receive_callback("on_confirm", _callback(), None))
    assert rejected.status_code == 401
    assert json.loads(rejected.body)["error"]["code"] == "UNAUTHORIZED_CALLBACK"
    assert calls["n"] == 0


def test_unknown_operation_returns_protocol_nack_not_transport_failure(monkeypatch):
    from app.routers import beckn as router_module

    class Store:
        async def record_callback(self, payload):
            return CallbackRecordResult(
                False,
                code="UNKNOWN_TRANSACTION",
                message="No matching Beckn operation",
            )

    monkeypatch.setattr(router_module.settings, "beckn_callback_token", "secret")
    monkeypatch.setattr(router_module, "get_beckn_operation_store", lambda: Store())

    rejected = asyncio.run(router_module.receive_callback("on_confirm", _callback(), "secret"))
    assert rejected.status_code == 200
    body = json.loads(rejected.body)
    assert body["message"]["ack"]["status"] == "NACK"
    assert body["error"]["code"] == "UNKNOWN_TRANSACTION"
