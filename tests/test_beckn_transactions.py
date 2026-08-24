import asyncio

import pytest

from app.services import beckn_transactions as bt


class MemoryStore:
    def __init__(self):
        self.operations = {}
        self.business = {}

    async def create_or_get(self, operation):
        transaction_id = self.business.get(operation.business_key_hash)
        if transaction_id:
            return self.operations[transaction_id].model_copy(deep=True), False
        self.business[operation.business_key_hash] = operation.transaction_id
        self.operations[operation.transaction_id] = operation.model_copy(deep=True)
        return operation.model_copy(deep=True), True

    async def get(self, transaction_id):
        operation = self.operations.get(transaction_id)
        return operation.model_copy(deep=True) if operation else None

    async def save(self, operation):
        self.operations[operation.transaction_id] = operation.model_copy(deep=True)


def _configure(monkeypatch):
    monkeypatch.setattr(bt.settings, "beckn_transaction_bridge_url", "https://bridge.test")
    monkeypatch.setattr(bt.settings, "beckn_transaction_bridge_token", "secret")
    monkeypatch.setattr(bt.settings, "beckn_callback_wait_seconds", 0.02)
    monkeypatch.setattr(bt.settings, "beckn_poll_interval_seconds", 0.001)


def _execute(facade, *, side_effecting=True):
    return facade.execute(
        session_id="session-1",
        subject_id="subject-1",
        tool_name="create_ai_call",
        domain="services:amul-vet-booking",
        action="confirm",
        message={"order": {"provider": {"id": "amul-ai-service"}}},
        business_key="session-1:ai-call",
        side_effecting=side_effecting,
    )


def _callback(operation, *, message_id=None, error=None):
    payload = {
        "context": {
            "domain": operation.domain,
            "action": "on_confirm",
            "bpp_id": operation.bpp_id,
            "transaction_id": operation.transaction_id,
            "message_id": message_id or operation.expected_callbacks["on_confirm"],
        },
        "message": {"order": {"id": "TICKET-1", "state": "ACTIVE"}},
    }
    if error:
        payload["error"] = error
    return payload


def test_fast_callback_wins_ack_race_and_completes(monkeypatch):
    _configure(monkeypatch)
    store = MemoryStore()
    facade = bt.BecknTransactionFacade(store)

    async def post_with_callback(operation, action, envelope):
        await facade.accept_callback("on_confirm", _callback(operation))
        return await store.get(operation.transaction_id)

    monkeypatch.setattr(facade, "_post_forward", post_with_callback)
    result = asyncio.run(_execute(facade))
    assert result.state == bt.OperationState.SUCCEEDED
    assert result.provider_order_id == "TICKET-1"


def test_duplicate_business_request_does_not_resubmit_confirm_and_uses_status(monkeypatch):
    _configure(monkeypatch)
    store = MemoryStore()
    facade = bt.BecknTransactionFacade(store)
    actions = []

    async def ack_without_callback(operation, action, envelope):
        actions.append(action)
        operation.state = bt.OperationState.ACKED_WAITING_CALLBACK
        await store.save(operation)
        return operation

    monkeypatch.setattr(facade, "_post_forward", ack_without_callback)
    first = asyncio.run(_execute(facade))
    second = asyncio.run(_execute(facade))

    assert first.state == bt.OperationState.TIMED_OUT_PENDING
    assert second.state == bt.OperationState.TIMED_OUT_PENDING
    assert actions == ["confirm", "status"]
    assert first.transaction_id == second.transaction_id


def test_callback_requires_exact_action_message_domain_and_bpp(monkeypatch):
    _configure(monkeypatch)
    store = MemoryStore()
    facade = bt.BecknTransactionFacade(store)

    async def ack_without_callback(operation, action, envelope):
        operation.state = bt.OperationState.ACKED_WAITING_CALLBACK
        await store.save(operation)
        return operation

    monkeypatch.setattr(facade, "_post_forward", ack_without_callback)
    pending = asyncio.run(_execute(facade))
    operation = asyncio.run(store.get(pending.transaction_id))

    with pytest.raises(bt.CallbackCorrelationError):
        asyncio.run(facade.accept_callback("on_confirm", _callback(operation, message_id="wrong")))

    payload = _callback(operation)
    payload["context"]["bpp_id"] = "attacker.example"
    with pytest.raises(bt.CallbackCorrelationError):
        asyncio.run(facade.accept_callback("on_confirm", payload))


def test_duplicate_callback_is_idempotently_accepted(monkeypatch):
    _configure(monkeypatch)
    store = MemoryStore()
    facade = bt.BecknTransactionFacade(store)

    async def ack_without_callback(operation, action, envelope):
        operation.state = bt.OperationState.ACKED_WAITING_CALLBACK
        await store.save(operation)
        return operation

    monkeypatch.setattr(facade, "_post_forward", ack_without_callback)
    pending = asyncio.run(_execute(facade))
    operation = asyncio.run(store.get(pending.transaction_id))
    payload = _callback(operation)

    first = asyncio.run(facade.accept_callback("on_confirm", payload))
    second = asyncio.run(facade.accept_callback("on_confirm", payload))
    assert first.state == second.state == bt.OperationState.SUCCEEDED
    assert len(second.callback_hashes) == 1

