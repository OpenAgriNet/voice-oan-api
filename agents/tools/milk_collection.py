"""Tool for fetching farmer milk collection and deduction details."""
import os

from pydantic import ValidationError

from app.models.milk_collection import FarmerMilkCollectionRequestModel
from agents.tools.farmer_animal_backends import get_farmer_milk_collection_details_api
from helpers.utils import get_logger

logger = get_logger(__name__)


def _num(value) -> str:
    """Render a numeric field compactly, dropping a trailing .0 (e.g. 2.0 -> '2')."""
    if value is None:
        return "unknown"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


_SHIFT_LABELS = {"M": "morning", "E": "evening"}


def _format_milk_collection_summary(response) -> str:
    """Deterministic plain-text summary for the voice agent.

    The voice agent runs on a small OSS model and speaks its answer aloud, so
    it gets a flat, labelled, per-record list (one record per line) instead of
    raw JSON or a markdown table. Each field is named so the model cannot
    confuse quantity with fat/SNF/amount when several records are present.
    """
    lines: list[str] = []

    if response.milk:
        lines.append(f"Milk collection records ({len(response.milk)}):")
        for i, r in enumerate(response.milk, 1):
            shift = _SHIFT_LABELS.get((r.shift or "").upper(), r.shift or "unknown")
            lines.append(
                f"  {i}. Date {r.date or 'unknown'}, {shift} shift: "
                f"quantity {_num(r.qty)} liters, "
                f"fat {_num(r.fat)}, SNF {_num(r.snf)}, "
                f"amount {_num(r.amount)} rupees."
            )
    else:
        lines.append("No milk collection records for the selected date range.")

    if response.deduction:
        lines.append(f"Deduction records ({len(response.deduction)}):")
        for i, d in enumerate(response.deduction, 1):
            lines.append(
                f"  {i}. Date {d.date or 'unknown'}, "
                f"{d.account_name or 'account'}: amount {_num(d.amount)} rupees."
            )
    else:
        lines.append("No deductions for the selected date range.")

    return "\n".join(lines)


async def get_farmer_milk_collection_details(
    union_code: str,
    society_code: str,
    farmer_code: str,
    fromdate: str,
    todate: str,
) -> str:
    """
    Fetch milk collection and deduction details for a farmer.

    Args:
        union_code: Union code for the farmer from farmer context.
        society_code: Society code for the farmer from farmer context.
        farmer_code: Farmer code for the farmer from farmer context.
        fromdate: Start date in YYYY-MM-DD format (ISO).
        todate: End date in YYYY-MM-DD format (ISO).

    Returns:
        str: Formatted milk collection and deduction details, or a clear failure message.
    """
    logger.info(
        "Milk collection tool invoked: union=%s society=%s farmer=%s fromdate=%s todate=%s",
        union_code,
        society_code,
        farmer_code,
        fromdate,
        todate,
    )

    token = os.getenv("PASHUGPT_TOKEN")
    if not token:
        logger.error("PASHUGPT_TOKEN is not set")
        return "Milk collection lookup failed. Service is not configured."

    try:
        request = FarmerMilkCollectionRequestModel(
            unionCode=union_code,
            societyCode=society_code,
            farmerCode=farmer_code,
            fromdate=fromdate,
            todate=todate,
        )
        request.validate_date_range()
    except (ValidationError, ValueError) as e:
        logger.info(
            "Milk collection lookup validation failed: union=%s society=%s farmer=%s fromdate=%s todate=%s error=%s",
            union_code,
            society_code,
            farmer_code,
            fromdate,
            todate,
            e,
        )
        return f"Milk collection lookup failed. {e}"

    response = await get_farmer_milk_collection_details_api(request, token)
    if response is None:
        logger.info(
            "Milk collection API failed: union=%s society=%s farmer=%s fromdate=%s todate=%s",
            union_code,
            society_code,
            farmer_code,
            fromdate,
            todate,
        )
        return "Milk collection lookup failed. Unable to fetch details at the moment."

    logger.info(
        "Milk collection lookup succeeded: union=%s society=%s farmer=%s from=%s to=%s milk_records=%s deductions=%s",
        union_code,
        society_code,
        farmer_code,
        fromdate,
        todate,
        len(response.milk),
        len(response.deduction),
    )
    formatted = _format_milk_collection_summary(response)
    return f"Milk collection details fetched successfully:\n\n{formatted}"
