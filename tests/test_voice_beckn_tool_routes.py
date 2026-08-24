import asyncio
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

from agents.deps import FarmerAccount, FarmerContext
from agents.tools import BASE_TOOLS, SIGNED_IN_FARMER_TOOLS
from agents.tools import beckn_voice as routes
from app.services.beckn_transactions import BecknResult, OperationState


class RecordingFacade:
    def __init__(self):
        self.calls = []

    async def execute(self, **kwargs):
        self.calls.append(kwargs)
        return BecknResult(
            state=OperationState.SUCCEEDED,
            transaction_id="tx-1",
            operation_id="op-1",
            payload={"message": {"order": {"id": "provider-1"}}},
        )


def _deps():
    return FarmerContext(
        query="test",
        session_id="session-1",
        signed_in=True,
        identity_verified=True,
        subject_id="subject-1",
        mobile="9924457046",
        farmer_accounts=[FarmerAccount(union_code="U1", society_code="S1", farmer_code="F1")],
        ai_technician_ids=["TECH-1"],
    )


def test_private_and_mutating_tools_are_not_in_anonymous_base_registry():
    base = {tool.function.__name__ for tool in BASE_TOOLS}
    signed = {tool.function.__name__ for tool in SIGNED_IN_FARMER_TOOLS}
    assert base == {"search_terms", "search_documents", "signal_conversation_state"}
    assert {
        "create_ai_call",
        "create_health_call",
        "get_farmer_milk_collection_details",
        "check_loan_eligibility",
        "get_union_scheme_data",
    }.issubset(signed)


def test_every_voice_business_route_uses_the_shared_facade(monkeypatch):
    recorder = RecordingFacade()
    monkeypatch.setattr(routes, "get_beckn_facade", lambda: recorder)
    deps = _deps()
    account = deps.farmer_accounts[0]

    async def run_all():
        await routes.beckn_document_search(deps, "mastitis", 5)
        await routes.beckn_ai_booking(deps, account, "TECH-1", "cow")
        await routes.beckn_health_booking(deps, account, "buffalo", "emergency", "fever")
        await routes.beckn_milk_statement(deps, [account], "2026-08-01", "2026-08-24")
        await routes.beckn_union_schemes(deps, "banas", "insurance")
        await routes.beckn_loan(deps, False)
        await routes.beckn_loan(deps, True)

    asyncio.run(run_all())
    assert [(call["domain"], call["action"]) for call in recorder.calls] == [
        ("advisory:amul-vet", "search"),
        ("services:amul-vet-booking", "confirm"),
        ("services:amul-vet-booking", "confirm"),
        ("services:amul-milk-collection", "init"),
        ("schemes:amul-union", "search"),
        ("finance:amul-microloan", "init"),
        ("finance:amul-microloan", "confirm"),
    ]
    assert recorder.calls[1]["side_effecting"] is True
    assert recorder.calls[2]["side_effecting"] is True
    assert recorder.calls[-1]["side_effecting"] is True

