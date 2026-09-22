"""Voice-side Beckn adapters with output shapes matching legacy backends."""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional

from pydantic import BaseModel, ConfigDict

from agents.models.ai_call import AICallRequestModel, AICallResponseModel
from agents.models.farmer import FarmerRecord
from agents.models.health_call import HealthCallRequestModel, HealthCallResponseModel
from agents.tools.beckn.operations import (
    BecknActionResult,
    OperationState,
    get_beckn_operation_client,
)
from app.models.milk_collection import FarmerMilkCollectionRequestModel, FarmerMilkCollectionResponseModel
from helpers.utils import get_logger

logger = get_logger(__name__)


class BecknProviderUnavailable(RuntimeError):
    """The BPP did not return a completed business response."""


class AITechnicianBySocietyRecord(BaseModel):
    """Compat model mirroring legacy voice backend technician rows."""

    model_config = ConfigDict(extra="allow")

    userId: Optional[str] = None
    fullName: Optional[str] = None
    mobileNumber: Optional[str] = None


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _code(value: Any) -> str:
    node = _record(value)
    descriptor = _record(node.get("descriptor"))
    return str(descriptor.get("code") or node.get("code") or "")


def _groups(tags: Any, code: str) -> list[dict[str, Any]]:
    if not isinstance(tags, list):
        return []
    return [tag for tag in tags if isinstance(tag, dict) and _code(tag) == code]


def _values(group: Mapping[str, Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    entries = group.get("list")
    if not isinstance(entries, list):
        return values
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("value") is None:
            continue
        values[_code(entry)] = str(entry["value"])
    return values


def _nested_values(group: Mapping[str, Any], code: str) -> list[str]:
    entries = group.get("list")
    if not isinstance(entries, list):
        return []
    out: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict) or _code(entry) != code:
            continue
        if entry.get("value") is not None:
            out.append(str(entry["value"]).strip())
        nested = entry.get("list")
        if isinstance(nested, list):
            out.extend(
                str(child.get("value")).strip()
                for child in nested
                if isinstance(child, dict) and child.get("value") is not None
            )
    return [value for value in out if value]


def _completed_payload(result: BecknActionResult, label: str) -> dict[str, Any]:
    operation = result.operation
    if operation.state in {OperationState.NACKED, OperationState.BUSINESS_FAILED}:
        error = _record(result.payload).get("error")
        message = _record(error).get("message") if isinstance(error, dict) else None
        raise BecknProviderUnavailable(str(message or f"{label} provider rejected the request"))
    if operation.state is OperationState.TIMED_OUT_PENDING:
        raise BecknProviderUnavailable(f"{label} callback is still pending")
    if operation.state is not OperationState.SUCCEEDED or not isinstance(result.payload, dict):
        raise BecknProviderUnavailable(f"{label} callback was not completed")
    return result.payload


def _order(payload: Mapping[str, Any]) -> dict[str, Any]:
    return _record(_record(payload.get("message")).get("order"))


def _providers(on_search: Any) -> list[dict]:
    catalog = (on_search or {}).get("message", {}).get("catalog", {}) if isinstance(on_search, dict) else {}
    return catalog.get("providers") or catalog.get("bpp/providers") or []


def _items(on_search: Any) -> list[dict]:
    return [item for provider in _providers(on_search) for item in provider.get("items", [])]


def _activity(value: Optional[str]) -> Optional[dict[str, Any]]:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {"summary": value}
    return parsed if isinstance(parsed, dict) else {"summary": value}


def _as_int(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


async def fetch_farmer_records_by_mobile(
    mobile: str,
    *,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> Optional[list[dict[str, Any]]]:
    """Return legacy-shaped farmer records, or None when no farmer is found."""
    payload = _completed_payload(
        await get_beckn_operation_client().init_farmer_profile(
            provider_id="amulpashudhan",
            mobile=mobile,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
        "farmer profile",
    )
    order = _order(payload)
    if order.get("state") == "NOT_FOUND":
        return None

    fulfillments = order.get("fulfillments")
    fulfillment = _record(fulfillments[0]) if isinstance(fulfillments, list) and fulfillments else {}
    customer = _record(fulfillment.get("customer"))
    person = _record(customer.get("person"))
    tags = person.get("tags")
    account_groups = _groups(tags, "farmer_accounts")
    if not account_groups:
        raise BecknProviderUnavailable("farmer profile callback did not contain farmer_accounts")

    global_tags = [
        value
        for group in _groups(tags, "animal_tags")
        for value in _nested_values(group, "tag_id")
    ]

    aliases = {
        "sub_district": "subDistrict",
        "union_name": "unionName",
        "union_code": "unionCode",
        "society_name": "societyName",
        "society_code": "societyCode",
        "farmer_name": "farmerName",
        "farmer_code": "farmerCode",
        "total_animals": "totalAnimals",
    }

    records: list[dict[str, Any]] = []
    for group in account_groups:
        raw = _values(group)
        mapped = {aliases.get(key, key): value for key, value in raw.items()}
        mapped["mobileNumber"] = mobile
        tag_ids = _nested_values(group, "tag_id") + _nested_values(group, "animal_tags")
        if not tag_ids and len(account_groups) == 1:
            tag_ids = global_tags
        if tag_ids:
            joined = ",".join(dict.fromkeys(tag_ids))
            mapped["tagNo"] = joined
            mapped["tagNumbers"] = joined
        if "totalAnimals" in mapped:
            mapped["totalAnimals"] = _as_int(mapped.get("totalAnimals"))
        # Validate against current voice model schema while preserving extra keys.
        records.append(FarmerRecord.model_validate(mapped).model_dump(exclude_none=True))
    return records


async def fetch_animal_profile_by_tag(
    tag_no: str,
    *,
    union_code: Optional[str] = None,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Return legacy-shaped animal details, or None when no animal is found."""
    payload = _completed_payload(
        await get_beckn_operation_client().init_animal_profile(
            provider_id="amulpashudhan",
            tag_id=tag_no,
            union_code=union_code,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
        "animal profile",
    )
    order = _order(payload)
    if order.get("state") == "NOT_FOUND":
        return None
    items = order.get("items")
    item = _record(items[0]) if isinstance(items, list) and items else {}
    tags = item.get("tags")
    groups = _groups(tags, "animal_profile")
    if not groups:
        return None

    raw = _values(groups[0])
    mapped = {
        "tagNumber": raw.get("tag_id"),
        "animalType": raw.get("animal_type"),
        "animalName": raw.get("animal_name"),
        "breed": raw.get("breed"),
        "milkingStage": raw.get("milking_stage"),
        "pregnancyStage": raw.get("pregnancy_stage"),
        "dateOfBirth": raw.get("date_of_birth"),
        "lactationNo": _as_int(raw.get("lactation_number")),
        "lastBreedingActivity": _activity(raw.get("last_breeding_activity")),
        "lastHealthActivity": _activity(raw.get("last_health_activity")),
    }
    return {key: value for key, value in mapped.items() if value is not None}


async def get_ai_technicians_by_society(
    *,
    union_code: str,
    society_code: str,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> list[AITechnicianBySocietyRecord]:
    """Return technician rows matching legacy voice fields."""
    payload = _completed_payload(
        await get_beckn_operation_client().search_ai_technicians(
            union_code=union_code,
            society_code=society_code,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
        "AI technician discovery",
    )
    out: list[AITechnicianBySocietyRecord] = []
    for item in _items(payload):
        item = _record(item)
        details = _groups(item.get("tags"), "technician-details")
        values = _values(details[0]) if details else {}
        item_id = str(item.get("id") or "")
        technician_id = values.get("technician_id") or (item_id[4:] if item_id.startswith("ait:") else None)
        name = _record(item.get("descriptor")).get("name")
        if isinstance(name, str) and name.endswith(" (AI technician)"):
            name = name.removesuffix(" (AI technician)")
        if technician_id:
            out.append(
                AITechnicianBySocietyRecord(
                    userId=technician_id,
                    fullName=str(name or ""),
                    mobileNumber=values.get("mobile"),
                )
            )
    return out


def _extract_ticket_and_ait_name(payload: Mapping[str, Any]) -> tuple[Optional[str], Optional[str]]:
    order = _order(payload)
    ticket = order.get("id")
    ait_name: Optional[str] = None
    fulfillments = order.get("fulfillments")
    if isinstance(fulfillments, list) and fulfillments:
        first = _record(fulfillments[0])
        agent = _record(first.get("agent"))
        person = _record(agent.get("person"))
        descriptor = _record(person.get("descriptor"))
        raw_name = descriptor.get("name") or person.get("name")
        if isinstance(raw_name, str):
            ait_name = raw_name
    return (str(ticket) if ticket is not None else None), ait_name


async def create_ai_call_booking(
    request: AICallRequestModel,
    *,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> AICallResponseModel:
    payload = _completed_payload(
        await get_beckn_operation_client().confirm_booking(
            service="ai-call",
            union_code=request.union_code,
            society_code=request.society_code,
            farmer_code=request.farmer_code,
            species=request.species.value,
            technician_id=request.user_id,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
        "AI booking",
    )
    ticket, ait_name = _extract_ticket_and_ait_name(payload)
    return AICallResponseModel(aitName=ait_name, ticketNumber=ticket)


async def create_health_call_booking(
    request: HealthCallRequestModel,
    *,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> HealthCallResponseModel:
    payload = _completed_payload(
        await get_beckn_operation_client().confirm_booking(
            service="health-call",
            union_code=request.union_code,
            society_code=request.society_code,
            farmer_code=request.farmer_code,
            species=request.species.value,
            case_type=request.case_type.value,
            remark=request.remark,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
        "health booking",
    )
    ticket, _ = _extract_ticket_and_ait_name(payload)
    return HealthCallResponseModel(ticketNumber=ticket)


async def get_farmer_milk_collection_details(
    request: FarmerMilkCollectionRequestModel,
    *,
    session_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
) -> FarmerMilkCollectionResponseModel:
    payload = _completed_payload(
        await get_beckn_operation_client().init_milk_collection(
            union_code=request.union_code,
            society_code=request.society_code,
            farmer_code=request.farmer_code,
            fromdate=request.fromdate,
            todate=request.todate,
            session_id=session_id,
            tool_call_id=tool_call_id,
        ),
        "milk collection",
    )
    order = _order(payload)
    items = order.get("items")
    item = _record(items[0]) if isinstance(items, list) and items else {}
    tags = item.get("tags")

    result = None
    period_groups = _groups(tags, "query-period")
    if period_groups:
        result = _values(period_groups[0]).get("result")

    milk = [_values(group) for group in _groups(tags, "milk-record")]
    deduction = []
    for group in _groups(tags, "deduction-record"):
        values = _values(group)
        if "account_name" in values and "accountname" not in values:
            values["accountname"] = values.pop("account_name")
        deduction.append(values)

    return FarmerMilkCollectionResponseModel.model_validate(
        {"result": result, "milk": milk, "deduction": deduction}
    )

