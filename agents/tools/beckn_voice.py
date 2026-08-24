"""Voice-tool mappings onto the shared Beckn transaction façade."""

from __future__ import annotations

import json
from typing import Any

from agents.deps import FarmerAccount, FarmerContext
from app.services.beckn_transactions import BecknResult, OperationState, get_beckn_facade


def _tag_group(code: str, values: dict[str, Any]) -> dict[str, Any]:
    return {
        "descriptor": {"code": code},
        "list": [
            {"descriptor": {"code": key}, "value": str(value)}
            for key, value in values.items()
            if value is not None
        ],
    }


def _private_customer(deps: FarmerContext) -> dict[str, Any]:
    return {"person": {"id": deps.subject_id}}


def render_result(result: BecknResult, capability: str) -> str:
    """Stable agent-facing wording for completed, pending, and error states."""
    if result.state == OperationState.SUCCEEDED:
        message = (result.payload or {}).get("message") or {}
        order = message.get("order") or {}
        if order.get("id"):
            return f"{capability} completed successfully. Provider reference: {order['id']}."
        return f"{capability} completed successfully:\n{json.dumps(message, ensure_ascii=False)}"
    if result.state in {
        OperationState.CREATED,
        OperationState.SENT,
        OperationState.ACKED_WAITING_CALLBACK,
        OperationState.TIMED_OUT_PENDING,
        OperationState.UNKNOWN_REQUIRES_RECONCILIATION,
    }:
        return (
            f"{capability} was accepted and is still pending provider confirmation. "
            f"Reference: {result.transaction_id}. Do not submit it again; its status will be reconciled."
        )
    error = result.error or {}
    detail = error.get("message") or error.get("code") or "The provider could not complete it."
    return f"{capability} was not completed. {detail} Reference: {result.transaction_id}."


async def beckn_document_search(deps: FarmerContext, query: str, top_k: int) -> BecknResult:
    return await get_beckn_facade().execute(
        session_id=deps.session_id or "no-session",
        subject_id=deps.subject_id or "public",
        tool_name="search_documents",
        domain="advisory:amul-vet",
        action="search",
        message={"intent": {"item": {"descriptor": {"name": query}}, "tags": [_tag_group("search-options", {"top_k": top_k})]}},
        business_key=f"{deps.session_id}:document-search:{query}:{top_k}",
    )


async def beckn_ai_booking(
    deps: FarmerContext,
    account: FarmerAccount,
    technician_id: str,
    species: str,
) -> BecknResult:
    message = {
        "order": {
            "provider": {"id": "amul-ai-service"},
            "items": [{"id": f"ait:{technician_id}"}],
            "fulfillments": [{
                "id": "fulfillment-1",
                "type": "TECHNICIAN_VISIT",
                "customer": _private_customer(deps),
                "stops": [{"location": {"descriptor": {"code": f"society:{account.society_code}"}}}],
                "tags": [_tag_group("booking-details", {
                    "union_code": account.union_code,
                    "farmer_code": account.farmer_code,
                    "species": species,
                })],
            }],
        }
    }
    return await get_beckn_facade().execute(
        session_id=deps.session_id or "no-session",
        subject_id=deps.subject_id or "",
        tool_name="create_ai_call",
        domain="services:amul-vet-booking",
        action="confirm",
        message=message,
        business_key=f"{deps.session_id}:ai-call",
        side_effecting=True,
    )


async def beckn_health_booking(
    deps: FarmerContext,
    account: FarmerAccount,
    species: str,
    case_type: str,
    remark: str | None,
) -> BecknResult:
    message = {
        "order": {
            "provider": {"id": "amul-animal-health-service"},
            "items": [{"id": "health-call"}],
            "fulfillments": [{
                "id": "fulfillment-1",
                "type": "VETERINARY_VISIT",
                "customer": _private_customer(deps),
                "tags": [_tag_group("health-call-details", {
                    "union_code": account.union_code,
                    "society_code": account.society_code,
                    "farmer_code": account.farmer_code,
                    "species": species,
                    "case_type": case_type,
                    "remark": remark,
                })],
            }],
        }
    }
    return await get_beckn_facade().execute(
        session_id=deps.session_id or "no-session",
        subject_id=deps.subject_id or "",
        tool_name="create_health_call",
        domain="services:amul-vet-booking",
        action="confirm",
        message=message,
        business_key=f"{deps.session_id}:health-call",
        side_effecting=True,
    )


async def beckn_milk_statement(
    deps: FarmerContext,
    accounts: list[FarmerAccount],
    fromdate: str,
    todate: str,
) -> BecknResult:
    fulfillments = []
    for index, account in enumerate(accounts, 1):
        fulfillments.append({
            "id": f"account-{index}",
            "customer": _private_customer(deps),
            "tags": [_tag_group("statement-request", {
                "union_code": account.union_code,
                "society_code": account.society_code,
                "farmer_code": account.farmer_code,
                "from_date": fromdate,
                "to_date": todate,
            })],
        })
    return await get_beckn_facade().execute(
        session_id=deps.session_id or "no-session",
        subject_id=deps.subject_id or "",
        tool_name="get_farmer_milk_collection_details",
        domain="services:amul-milk-collection",
        action="init",
        message={"order": {
            "provider": {"id": "amul-milk-data-service"},
            "items": [{"id": "milk-collection-statement"}],
            "fulfillments": fulfillments,
        }},
        business_key=f"{deps.session_id}:milk:{fromdate}:{todate}",
    )


async def beckn_union_schemes(
    deps: FarmerContext,
    union_name: str,
    scheme_name: str | None,
) -> BecknResult:
    return await get_beckn_facade().execute(
        session_id=deps.session_id or "no-session",
        subject_id=deps.subject_id or "",
        tool_name="get_union_scheme_data",
        domain="schemes:amul-union",
        action="search",
        message={"intent": {
            "category": {"descriptor": {"code": "milk-producer-schemes"}},
            "item": {"descriptor": {"name": scheme_name or ""}},
            "provider": {"descriptor": {"code": union_name}},
        }},
        business_key=f"{deps.session_id}:union-schemes:{union_name}:{scheme_name or '*'}",
    )


async def beckn_loan(deps: FarmerContext, confirmed: bool) -> BecknResult:
    action = "confirm" if confirmed else "init"
    item_id = "microloan-issuance" if confirmed else "microloan-eligibility"
    return await get_beckn_facade().execute(
        session_id=deps.session_id or "no-session",
        subject_id=deps.subject_id or "",
        tool_name="check_loan_eligibility",
        domain="finance:amul-microloan",
        action=action,
        message={"order": {
            "provider": {"id": "amul-cooperative-loan-service"},
            "items": [{"id": item_id}],
            "fulfillments": [{"customer": _private_customer(deps)}],
        }},
        business_key=f"{deps.session_id}:loan:{action}",
        side_effecting=confirmed,
    )
