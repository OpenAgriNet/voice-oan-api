from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from dotenv import load_dotenv

from app.config import get_default_httpx_timeout
from app.core.cache import cache
from agents.deps import FarmerContext
from helpers.utils import get_logger
from pydantic_ai.tools import RunContext

load_dotenv()

logger = get_logger(__name__)

SATHI_CACHE_NS = "sathi"
SATHI_MASTER_BASE = os.getenv("SATHI_MASTER_BASE_URL", "").rstrip("/")
SATHI_MASTER_API_KEY = os.getenv("SATHI_MASTER_API_KEY")
CROP_GROUPS_CACHE_KEY = "crop_groups"
# Cap dealers in one tool return (very large catalogs).
MAX_SATHI_DEALERS_IN_SEARCH = 60
# Cap variety names listed per dealer when many lots map to the same dealer.
MAX_VARIETIES_PER_DEALER = 15

# ---------------------------------------------------------------------------
# Master API helpers
# ---------------------------------------------------------------------------

def _unwrap_master_payload(body: dict[str, Any]) -> tuple[int, str, list]:
    enc = body.get("EncryptedResponse")
    payload = enc if isinstance(enc, dict) else body
    status = int(payload.get("status_code") or payload.get("statusCode") or 0)
    message = str(payload.get("message") or "")
    data = payload.get("data")
    return status, message, data if isinstance(data, list) else []


# ---------------------------------------------------------------------------
# Tag / section parsing
# ---------------------------------------------------------------------------

def _seed_tag_bucket(tag: dict[str, Any]) -> str | None:
    desc = tag.get("descriptor")
    if not isinstance(desc, dict):
        return None
    code = str(desc.get("code") or "").strip().lower()
    name = str(desc.get("name") or "").strip().lower()
    if code == "seed-details" or name == "seed details":
        return "seed-details"
    if code == "dealer-list" or name == "available dealers":
        return "dealer-list"
    return None


def _extract_seed_tag_sections(tags: Any) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"seed-details": [], "dealer-list": []}
    if not isinstance(tags, list):
        return out
    for t in tags:
        if not isinstance(t, dict):
            continue
        bucket = _seed_tag_bucket(t)
        if bucket not in out:
            continue
        lst = t.get("list")
        if isinstance(lst, list):
            out[bucket].extend(x for x in lst if isinstance(x, dict))
    return out


def _seed_detail_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    m: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = row.get("descriptor") if isinstance(row.get("descriptor"), dict) else {}
        code = str(d.get("code") or "").strip()
        if code:
            val = row.get("value")
            m[code] = "" if val is None else str(val)
    return m


# ---------------------------------------------------------------------------
# Dealer parsing
# ---------------------------------------------------------------------------

def _parse_dealer(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            p = json.loads(raw.strip())
            return p if isinstance(p, dict) else {"value": p}
        except json.JSONDecodeError:
            return {"value": raw}
    return {}


def _dealer_key(d: dict[str, Any]) -> str:
    did = str(d.get("dealer_id") or "").strip()
    if did:
        return f"id:{did}"
    return f"name:{str(d.get('dealer_name') or '').strip()}|dist:{str(d.get('district') or '').strip()}"


def _num(val: Any, as_int: bool) -> float | int:
    try:
        return int(float(val)) if as_int else float(val)
    except (TypeError, ValueError):
        return 0 if as_int else 0.0


# ---------------------------------------------------------------------------
# Beckn catalog helpers
# ---------------------------------------------------------------------------

def _all_response_catalogs(data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for resp in data.get("responses") or []:
        if not isinstance(resp, dict):
            continue
        cat = (resp.get("message") or {}).get("catalog")
        if isinstance(cat, dict) and cat:
            out.append(cat)
    return out


def _find_matching_catalog(data: dict[str, Any], crop_code: str | None) -> dict[str, Any]:
    """Pick the catalog whose search-context matches this request (avoids stale bundled responses)."""
    all_catalogs = _all_response_catalogs(data)
    if not all_catalogs:
        msg = data.get("message")
        if isinstance(msg, dict):
            cat = msg.get("catalog")
            if isinstance(cat, dict):
                return cat
        return {}

    cc_req = (crop_code or "").strip().upper() or None

    if cc_req:
        for cat in all_catalogs:
            ctx = _catalog_search_context_map(cat)
            if str(ctx.get("crop-code") or "").strip().upper() != cc_req:
                continue
            if str(ctx.get("status") or "").strip().lower() == "success":
                return cat
        for cat in all_catalogs:
            ctx = _catalog_search_context_map(cat)
            if str(ctx.get("crop-code") or "").strip().upper() == cc_req:
                return cat
        return {}

    for cat in all_catalogs:
        ctx = _catalog_search_context_map(cat)
        if str(ctx.get("status") or "").strip().lower() == "success":
            return cat

    return all_catalogs[0]


def _beckn_flatten_providers_from_catalog(
    data: dict[str, Any], target_catalog: dict[str, Any]
) -> list[dict[str, Any]]:
    """Providers for the matching catalog (same object, or same crop/district in another response)."""
    p = target_catalog.get("providers")
    if isinstance(p, list) and p:
        return [x for x in p if isinstance(x, dict)]

    target_ctx = _catalog_search_context_map(target_catalog)
    target_crop = str(target_ctx.get("crop-code") or "").strip().upper()
    target_district = str(target_ctx.get("district-code") or "").strip()
    if not target_crop:
        return []

    for resp in data.get("responses") or []:
        if not isinstance(resp, dict):
            continue
        cat = (resp.get("message") or {}).get("catalog")
        if not isinstance(cat, dict):
            continue
        ctx = _catalog_search_context_map(cat)
        if str(ctx.get("crop-code") or "").strip().upper() != target_crop:
            continue
        if target_district:
            cd = str(ctx.get("district-code") or "").strip()
            if cd and cd != target_district:
                continue
        pl = cat.get("providers")
        if isinstance(pl, list):
            return [x for x in pl if isinstance(x, dict)]
    return []


def _catalog_search_context_map(catalog: dict[str, Any]) -> dict[str, str]:
    tags = catalog.get("tags")
    if not isinstance(tags, list):
        return {}
    for t in tags:
        if not isinstance(t, dict):
            continue
        desc = t.get("descriptor") if isinstance(t.get("descriptor"), dict) else {}
        code = str(desc.get("code") or "").strip().lower()
        name = str(desc.get("name") or "").strip().lower()
        if code != "search-context" and name != "search context":
            continue
        lst = t.get("list")
        if not isinstance(lst, list):
            return {}
        return _seed_detail_map([x for x in lst if isinstance(x, dict)])
    return {}


def _get_search_context_status(catalog: dict[str, Any]) -> tuple[bool, str]:
    """Returns (is_success, message) from search context tags.

    If ``status`` is absent (older payloads), treat as success so we still render ``providers``.
    """
    ctx = _catalog_search_context_map(catalog)
    if "status" not in ctx:
        return True, ""
    status = str(ctx.get("status") or "").strip().lower()
    message = str(ctx.get("message") or "").strip()
    return status == "success", message


def _seed_availability_data_not_available_response(location_str: str) -> str:
    """Farmer-safe line when search failed or catalog has no providers — no HTTP text or crop codes."""
    place = (location_str or "").strip() or "this location"
    return (
        f"Data is not available for certified seed stock in {place}.\n\n"
        "**Source: SATHI**"
    )


def _merge_providers_by_variety(providers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for p in providers:
        desc = p.get("descriptor") if isinstance(p.get("descriptor"), dict) else {}
        key = (str(p.get("id") or ""), str(desc.get("name") or ""))
        if key not in by_key:
            by_key[key] = {"id": p.get("id"), "descriptor": desc, "items": []}
            order.append(key)
        items = p.get("items")
        if isinstance(items, list):
            by_key[key]["items"].extend(x for x in items if isinstance(x, dict))
    return [by_key[k] for k in order]


# ---------------------------------------------------------------------------
# Dealer accumulation
# ---------------------------------------------------------------------------

def _accumulate_dealer(
    dealer_acc: dict[str, dict[str, Any]],
    row: dict[str, Any],
    variety: str = "",
) -> None:
    """
    Parse a dealer-list row and merge it into dealer_acc.
    Skips rows with zero or negative bag counts.
    ``variety`` is the provider/variety label for this item's dealer rows.
    """
    dealer = dict(_parse_dealer(row.get("value")))
    rd = row.get("descriptor") if isinstance(row.get("descriptor"), dict) else {}
    tid = str(rd.get("code") or "").strip()
    tname = str(rd.get("name") or "").strip()

    if tid and not dealer.get("dealer_id"):
        dealer["dealer_id"] = tid
    if tname and not dealer.get("dealer_name"):
        dealer["dealer_name"] = tname

    if not dealer.get("dealer_id") and not dealer.get("dealer_name"):
        return

    b_add = _num(dealer.get("bags"), True)
    q_add = _num(dealer.get("quintals"), False)

    # Skip zero / noise stock rows
    if int(b_add) <= 0:
        return

    dk = _dealer_key(dealer)
    v = (variety or "").strip()
    if v == "(unknown variety)":
        v = ""

    if dk not in dealer_acc:
        dealer_acc[dk] = {
            "dealer_name": str(dealer.get("dealer_name") or "").strip() or "(unknown)",
            "district": str(dealer.get("district") or "").strip(),
            "state": str(dealer.get("state") or "").strip(),
            "contact_number": str(dealer.get("contact_number") or "").strip(),
            "bags": int(b_add),
            "quintals": float(q_add),
            "varieties": [v] if v else [],
        }
    else:
        a = dealer_acc[dk]
        a["bags"] = int(a["bags"]) + int(b_add)
        a["quintals"] = float(a["quintals"]) + float(q_add)
        if v:
            vs = a.setdefault("varieties", [])
            if v not in vs:
                vs.append(v)


# ---------------------------------------------------------------------------
# Main formatters (dealer list for seed availability)
# ---------------------------------------------------------------------------

def _format_dealer_details(
    dealer_acc: dict[str, dict[str, Any]],
    location_str: str,
    *,
    include_source_footer: bool = True,
    max_dealers: int | None = MAX_SATHI_DEALERS_IN_SEARCH,
) -> str:
    active = {k: v for k, v in dealer_acc.items() if v.get("bags", 0) > 0}
    if not active:
        return "No dealer contact details available for this crop in this location."

    rows = sorted(active.values(), key=lambda x: int(x.get("bags", 0)), reverse=True)
    omitted = 0
    if max_dealers is not None and len(rows) > max_dealers:
        omitted = len(rows) - max_dealers
        rows = rows[:max_dealers]

    lines = [f"**Dealers with stock in {location_str}**", ""]
    for d in rows:
        dist = d.get("district") or ""
        st = d.get("state") or ""
        place = ", ".join(x for x in (dist, st) if x) or "—"
        contact = d.get("contact_number") or "Not available"
        bags = d.get("bags", 0)
        q = float(d.get("quintals", 0))
        name = d.get("dealer_name") or "(unknown)"

        lines.append(f"**{name}**")
        lines.append(f"District: {place}")
        lines.append(f"Contact: {contact}")
        lines.append(f"Total stock: {bags} bags ({q:g} quintals)")
        vars_ = d.get("varieties")
        if isinstance(vars_, list) and vars_:
            shown = vars_[:MAX_VARIETIES_PER_DEALER]
            more = len(vars_) - MAX_VARIETIES_PER_DEALER
            suf = f" (+{more} more)" if more > 0 else ""
            lines.append(f"Variety: {', '.join(shown)}{suf}")
        lines.append("")

    if include_source_footer:
        lines.append("**Source: SATHI**")
    out = "\n".join(lines).rstrip()
    if omitted:
        out += (
            f"\n\n**Note:** {omitted} more dealer(s) with stock in this area are not listed above — "
            "the farmer can try another district or crop for a fresh search."
        )
    return out


def _format_seed_search_response(
    data: dict[str, Any], crop_code: str | None = None
) -> str:
    """Build dealer-list text (contacts, stock, varieties) for the seed search tool return."""
    if not isinstance(data, dict):
        data = {}

    catalog = _find_matching_catalog(data, crop_code)
    requested_cc = (crop_code or "").strip()
    if not catalog and requested_cc:
        return (
            f"No catalog in this response matched crop {requested_cc.upper()}. "
            "The network may have bundled older callbacks; try the search again.\n\n"
            "**Source: SATHI**"
        )

    providers = _beckn_flatten_providers_from_catalog(data, catalog)

    is_success, _api_message = _get_search_context_status(catalog)
    ctx = _catalog_search_context_map(catalog)
    crop_code_ctx = str(ctx.get("crop-code") or "").strip()
    district = str(ctx.get("district-name") or "").strip()
    state = str(ctx.get("state-name") or "").strip()
    location_str = ", ".join(x for x in (district, state) if x) or "the requested location"
    crop_disp = crop_code_ctx or requested_cc
    crop_suffix = f", crop {crop_disp}" if crop_disp else ""

    if not is_success:
        return _seed_availability_data_not_available_response(location_str)

    if not providers:
        return _seed_availability_data_not_available_response(location_str)

    merged = _merge_providers_by_variety(providers)
    dealer_acc: dict[str, dict[str, Any]] = {}

    for p in merged:
        desc = p.get("descriptor") if isinstance(p.get("descriptor"), dict) else {}
        variety = str(desc.get("name") or "").strip() or "(unknown variety)"
        items = p.get("items") if isinstance(p.get("items"), list) else []

        for it in items:
            if not isinstance(it, dict):
                continue
            sections = _extract_seed_tag_sections(it.get("tags"))
            for row in sections.get("dealer-list") or []:
                if isinstance(row, dict):
                    _accumulate_dealer(dealer_acc, row, variety)

    active_dealers = {k: v for k, v in dealer_acc.items() if v.get("bags", 0) > 0}
    if not active_dealers:
        return (
            f"No dealers with stock found for this crop in {location_str}{crop_suffix}.\n\n"
            "**Source: SATHI**"
        )

    return _format_dealer_details(
        dealer_acc, location_str, include_source_footer=True, max_dealers=MAX_SATHI_DEALERS_IN_SEARCH
    )


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

async def _http_get_json(url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=get_default_httpx_timeout()) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


# ---------------------------------------------------------------------------
# Public tool functions
# ---------------------------------------------------------------------------


async def get_sathi_crop_groups() -> str:
    """Official SATHI crop groups (group_code, group_name); use group_code with list_sathi_crops_in_group."""
    try:
        cached = await cache.get(CROP_GROUPS_CACHE_KEY, namespace=SATHI_CACHE_NS)
        if isinstance(cached, list):
            rows = cached
        else:
            rows = await _fetch_crop_groups_from_api()
            await cache.set(
                CROP_GROUPS_CACHE_KEY,
                rows,
                ttl=7 * 24 * 3600,
                namespace=SATHI_CACHE_NS,
            )

        active = [g for g in rows if g.get("is_active") in (1, "1", True)] or rows
        lines = ["SATHI crop groups (use group_code with list_sathi_crops_in_group):", ""]
        for g in sorted(active, key=lambda x: str(x.get("group_code") or "")):
            gc, gn = g.get("group_code"), g.get("group_name")
            if gc and gn:
                lines.append(f"- group_code={gc} | group_name={gn}")
        if len(lines) <= 2:
            return "No crop groups returned from SATHI master API."
        return "\n".join(lines)
    except Exception:
        logger.exception("SATHI get_sathi_crop_groups failed")
        return "Could not load SATHI crop groups. Please try again later."


async def list_sathi_crops_in_group(group_code: str) -> str:
    """Crops in a SATHI group; pick crop_code for search_sathi_seed_availability."""
    gc = (group_code or "").strip().upper()
    if not gc:
        return "group_code is required (e.g. A02 from get_sathi_crop_groups)."

    try:
        rows = await _fetch_crops_for_group_from_api(gc)
        lines = [f"SATHI crops in group {gc} (pick crop_code for search_sathi_seed_availability):", ""]
        for r in sorted(rows, key=lambda x: str(x.get("crop_code") or "")):
            cc, cn = r.get("crop_code"), r.get("crop_name")
            if cc and cn:
                lines.append(f"- crop_code={cc} | crop_name={cn}")
        if len(lines) <= 2:
            return f"No crops listed for group {gc}. Check the group_code or try another group."
        return "\n".join(lines)
    except Exception:
        logger.exception("SATHI list_sathi_crops_in_group failed")
        return f"Could not load crops for group {gc} from SATHI. Please try again later."


async def _fetch_crop_groups_from_api() -> list[dict[str, Any]]:
    if not SATHI_MASTER_BASE:
        raise RuntimeError("SATHI_MASTER_BASE_URL is not configured")
    body = await _http_get_json(f"{SATHI_MASTER_BASE}/get-crop-group")
    status, message, rows = _unwrap_master_payload(body)
    if status != 200:
        raise RuntimeError(f"SATHI crop groups API error: {status} — {message}")
    return [r for r in rows if isinstance(r, dict)]


async def _fetch_crops_for_group_from_api(group_code: str) -> list[dict[str, Any]]:
    if not SATHI_MASTER_BASE:
        raise RuntimeError("SATHI_MASTER_BASE_URL is not configured")
    if not SATHI_MASTER_API_KEY:
        raise RuntimeError("SATHI_MASTER_API_KEY is not configured")
    body = await _http_get_json(
        f"{SATHI_MASTER_BASE}/get-crops-list",
        params={"apiKey": SATHI_MASTER_API_KEY, "group_code": group_code.strip().upper()},
    )
    status, message, rows = _unwrap_master_payload(body)
    if status != 200:
        raise RuntimeError(f"SATHI crops list API error: {status} — {message}")
    return [r for r in rows if isinstance(r, dict)]


def _build_beckn_seed_search_payload(
    crop_code: str,
    latitude: float,
    longitude: float,
    session_id: str = "",
    question_id: str = "",
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "context": {
            "domain": "schemes:vistaar",
            "action": "search",
            "version": "1.1.0",
            "bap_id": os.getenv("BAP_ID"),
            "bap_uri": os.getenv("BAP_URI"),
            "bpp_id": os.getenv("BPP_ID"),
            "bpp_uri": os.getenv("BPP_URI"),
            "transaction_id": str(uuid.uuid4()),
            "message_id": str(uuid.uuid4()),
            "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z",
            "ttl": "PT10M",
            "location": {"country": {"code": "IND"}, "city": {"code": "*"}},
            "tags": {
                "session_id": session_id,
                "question_id": question_id,
            },
        },
        "message": {
            "intent": {
                "provider": {"id": "sathi-seed", "descriptor": {"code": "sathi-seed"}},
                "item": {
                    "descriptor": {"code": "seed_availability"},
                    "tags": [
                        {"descriptor": {"code": "crop_code"}, "value": crop_code.strip().upper()},
                        {"location": {"lat": float(latitude), "lon": float(longitude)}},
                    ],
                },
            }
        },
    }


async def search_sathi_seed_availability(
    ctx: RunContext[FarmerContext],
    crop_code: str,
    latitude: float,
    longitude: float,
) -> str:
    """SATHI certified seed dealers with stock for ``crop_code`` at ``latitude``, ``longitude``.

    Returns dealer names, district, contact, aggregated bags/quintals, variety labels, and
    **Source: SATHI**. Dealer rows are capped when the catalog is very large.
    """
    cc = (crop_code or "").strip().upper()
    if not cc:
        return "crop_code is required from the SATHI crop list."

    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        logger.error("BAP_ENDPOINT is not set")
        return "Seed availability service is not configured (BAP_ENDPOINT missing)."

    bep = bap_endpoint.rstrip("/")
    search_url = bep if bep.endswith("/search") else (bep + "/search")

    try:
        payload = _build_beckn_seed_search_payload(
            cc,
            latitude,
            longitude,
            session_id=ctx.deps.session_id,
            question_id=ctx.deps.question_id,
        )
        async with httpx.AsyncClient(timeout=get_default_httpx_timeout()) as client:
            response = await client.post(search_url, json=payload)

        if response.status_code != 200:
            logger.error(
                "SATHI Beckn search status %s for %s — %s",
                response.status_code,
                search_url,
                (response.text or "")[:500],
            )
            return "Seed availability service returned an error. Please try again later."

        return _format_seed_search_response(response.json(), crop_code=cc)

    except httpx.TimeoutException:
        logger.error("SATHI Beckn search timed out for %s", search_url)
        return (
            f"Seed availability request timed out (endpoint: {search_url}). "
            "Please try again later."
        )
    except httpx.RequestError as e:
        logger.error("SATHI Beckn search request failed: %s", e)
        return "Could not reach seed availability service. Please try again later."
    except Exception:
        logger.exception("SATHI search_sathi_seed_availability failed")
        return "Unexpected error while fetching seed availability. Please try again later."
