from __future__ import annotations
"""
GFR (Government Fertilizer Recommendation) tool for crop registry and fertilizer advice
using the Vistaar Beckn API.
"""

import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic_ai import ModelRetry

from app.config import get_default_httpx_timeout
from agents.deps import FarmerContext
from agents.tools.pmfby_scheme_status import normalize_phone_for_api
from helpers.utils import get_logger, to_ascii_digits
from pydantic_ai.tools import RunContext

load_dotenv()

logger = get_logger(__name__)


class GfrCropRegistrySearch(BaseModel):
    """Beckn /search body for GFR crop registry search."""

    latitude: float = Field(..., description="Farm latitude")
    longitude: float = Field(..., description="Farm longitude")
    session_id: str = ""
    question_id: str = ""

    def get_payload(self) -> Dict[str, Any]:
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
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ttl": "PT10M",
                "location": {"country": {"code": "IND"}, "city": {"code": "*"}},
                "tags": {
                    "session_id": self.session_id,
                    "question_id": self.question_id,
                },
            },
            "message": {
                "order": {
                    "provider": {"id": "gfr-agri"},
                    "items": [{"id": "gfr-agri-crop-registy"}],
                    "fulfillments": [
                        {
                            "customer": {
                                "person": {
                                    "tags": [
                                        {"location": {"lat": float(self.latitude), "lon": float(self.longitude)}}
                                    ]
                                }
                            }
                        }
                    ],
                }
            },
        }


class GfrRecommendationSearch(BaseModel):
    """Beckn /search body for GFR crop recommendation."""

    state_id: str
    crops: List[str]
    phone_no: str = Field(..., description="+91… normalized before construction")
    cycle: str
    district_id: Optional[str] = None
    # BPP returns chemical+organic dose rows when false; true returns NF practice text (recommendations array).
    natural_farming: bool = False
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    session_id: str = ""
    question_id: str = ""

    def get_payload(self) -> Dict[str, Any]:
        tags: List[Dict[str, Any]] = []
        if self.latitude is not None and self.longitude is not None:
            tags.append({"location": {"lat": float(self.latitude), "lon": float(self.longitude)}})
        tags.extend(
            [
                {"descriptor": {"code": "stateId"}, "value": self.state_id.strip()},
                {"descriptor": {"code": "crops"}, "value": list(self.crops)},
                {"descriptor": {"code": "naturalFarming"}, "value": bool(self.natural_farming)},
                {"descriptor": {"code": "phoneNo"}, "value": self.phone_no.strip()},
                {"descriptor": {"code": "cycle"}, "value": self.cycle.strip()},
            ]
        )
        if self.district_id:
            tags.append({"descriptor": {"code": "districtId"}, "value": self.district_id.strip()})

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
                "timestamp": now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                "ttl": "PT10M",
                "location": {"country": {"code": "IND"}, "city": {"code": "*"}},
                "tags": {
                    "session_id": self.session_id,
                    "question_id": self.question_id,
                },
            },
            "message": {
                "order": {
                    "provider": {"id": "gfr-agri"},
                    "items": [{"id": "gfr-agri-crop-recommendation"}],
                    "fulfillments": [{"customer": {"person": {"tags": tags}}}],
                }
            },
        }


class GfrDescriptor(BaseModel):
    model_config = ConfigDict(extra="ignore")
    code: Optional[str] = None
    name: Optional[str] = None
    long_desc: Optional[str] = None


class GfrProvider(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: Optional[str] = None
    descriptor: Optional[GfrDescriptor] = None
    items: Optional[List[Dict[str, Any]]] = None


class GfrCatalog(BaseModel):
    model_config = ConfigDict(extra="ignore")
    descriptor: Optional[GfrDescriptor] = None
    providers: Optional[List[GfrProvider]] = None

    def iter_raw_items(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for p in self.providers or []:
            for it in p.items or []:
                if isinstance(it, dict):
                    out.append(it)
        return out


class GfrCatalogMessage(BaseModel):
    model_config = ConfigDict(extra="ignore")
    catalog: Optional[GfrCatalog] = None


class GfrResponseEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")
    context: Optional[Dict[str, Any]] = None
    message: Optional[GfrCatalogMessage] = None


class GfrClientSearchResponse(BaseModel):
    """BAP client JSON: optional `context` + `responses[].message.catalog`."""

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _coerce_responses(cls, data: Any) -> Any:
        if isinstance(data, dict) and data.get("responses") is None:
            return {**data, "responses": []}
        return data

    context: Optional[Dict[str, Any]] = None
    responses: List[GfrResponseEnvelope] = Field(default_factory=list)

    def iter_raw_items(self) -> List[Dict[str, Any]]:
        found: List[Dict[str, Any]] = []
        for r in self.responses:
            msg = r.message
            if msg and msg.catalog:
                found.extend(msg.catalog.iter_raw_items())
        return found


class GfrRegistryItem(BaseModel):
    """One crop row from registry `items[]` (descriptor + `crop_details` tag list)."""

    model_config = ConfigDict(extra="ignore")
    id: Optional[str] = None
    crop_name: str = ""
    combined_name: str = ""
    variety: str = ""
    irrigation_type: str = ""
    season: str = ""
    splitdose: Optional[bool] = None
    gfr_available: Optional[bool] = None
    state_id: Optional[str] = None
    state_name: Optional[str] = None
    state_code: Optional[str] = None
    district_id: Optional[str] = None
    district_name: Optional[str] = None

    @staticmethod
    def _yes_no(value: Any) -> Optional[bool]:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        s = str(value).strip().lower()
        if s in ("yes", "true", "y", "1"):
            return True
        if s in ("no", "false", "n", "0"):
            return False
        return None

    @staticmethod
    def _crop_details_dict(tags: Any) -> Dict[str, Any]:
        if not isinstance(tags, list):
            return {}
        out: Dict[str, Any] = {}
        for tag in tags:
            if not isinstance(tag, dict):
                continue
            desc = tag.get("descriptor") or {}
            if desc.get("code") != "crop_details":
                continue
            li = tag.get("list")
            if not isinstance(li, list):
                continue
            for item in li:
                if not isinstance(item, dict):
                    continue
                code = (item.get("descriptor") or {}).get("code")
                if code:
                    out[str(code)] = item.get("value")
        return out

    @model_validator(mode="before")
    @classmethod
    def from_beckn(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return {}
        desc = data.get("descriptor") or {}
        crop_name = desc.get("name") or ""
        combined = desc.get("long_desc") or crop_name or data.get("id") or ""
        det = cls._crop_details_dict(data.get("tags"))
        return {
            "id": data.get("id"),
            "crop_name": crop_name,
            "combined_name": combined,
            "variety": str(det.get("variety") or ""),
            "irrigation_type": str(det.get("irrigationType") or ""),
            "season": str(det.get("season") or ""),
            "splitdose": cls._yes_no(det.get("splitdose")),
            "gfr_available": cls._yes_no(det.get("GFRavailable")),
            "state_id": det.get("stateId"),
            "state_name": det.get("stateName"),
            "state_code": det.get("stateCode"),
            "district_id": det.get("districtId"),
            "district_name": det.get("districtName"),
        }


class GfrFertilizerDoseItem(BaseModel):
    """One chemical product line inside fertilizersdata / fertilizersdatacombTwo."""

    model_config = ConfigDict(extra="ignore")
    name: Optional[str] = None
    values: Optional[Any] = None
    value: Optional[Any] = None
    unit: Optional[str] = None

    def amount(self) -> Any:
        return self.values if self.values is not None else self.value

    def display_name(self) -> str:
        n = (self.name or "").strip()
        return n if n else "Fertilizer"


class GfrNaturalFarmingRecoItem(BaseModel):
    """One practice row when naturalFarming=true on the request."""

    model_config = ConfigDict(extra="ignore")
    component: Optional[str] = None
    recommendation: Optional[str] = None
    purpose: Optional[str] = None


class GfrOrganicFertilizerBlock(BaseModel):
    """Organic quantities object on the recommendation row."""

    model_config = ConfigDict(extra="ignore")
    fym: Optional[str] = None
    fymUnit: Optional[str] = None
    compost: Optional[str] = None
    compostUnit: Optional[str] = None
    vermicompost: Optional[str] = None
    vermicompostUnit: Optional[str] = None
    oilCake: Optional[str] = None
    oilCakeUnit: Optional[str] = None
    bioFertilizers: Optional[str] = None
    method: Optional[str] = None


def _parse_fertilizer_dose_list(raw: Any) -> Optional[List[GfrFertilizerDoseItem]]:
    if not isinstance(raw, list):
        return None
    items: List[GfrFertilizerDoseItem] = []
    for it in raw[:10]:
        if isinstance(it, dict):
            try:
                items.append(GfrFertilizerDoseItem.model_validate(it))
            except ValidationError:
                continue
    return items if items else None


def _parse_nf_reco_list(raw: Any) -> Optional[List[GfrNaturalFarmingRecoItem]]:
    if not isinstance(raw, list):
        return None
    items: List[GfrNaturalFarmingRecoItem] = []
    for it in raw[:25]:
        if isinstance(it, dict):
            try:
                items.append(GfrNaturalFarmingRecoItem.model_validate(it))
            except ValidationError:
                continue
    return items if items else None


class GfrRecoPayloadRow(BaseModel):
    """One object from the BPP embedded `data` JSON array; `__str__` returns the formatted bullet block."""

    model_config = ConfigDict(extra="ignore")

    crop: Optional[str] = None
    fertilizersdata: Optional[List[GfrFertilizerDoseItem]] = None
    fertilizersdatacombTwo: Optional[List[GfrFertilizerDoseItem]] = None
    organicFertilizer: Optional[GfrOrganicFertilizerBlock] = None
    recommendations: Optional[List[GfrNaturalFarmingRecoItem]] = None

    @model_validator(mode="before")
    @classmethod
    def _normalize_wire(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        norm: Dict[str, Any] = {}
        for k, v in data.items():
            kl = str(k).lower().replace("_", "")
            if kl == "fertilizersdata":
                norm["fertilizersdata"] = v
            elif kl in ("fertilizersdatacombtwo", "fertilizersdatacomb2", "fertilizerdatacomb2"):
                norm["fertilizersdatacombTwo"] = v
            elif kl == "organicfertilizer":
                norm["organicFertilizer"] = v
            elif kl == "recommendations":
                norm["recommendations"] = v
            elif kl == "crop":
                norm["crop"] = v

        norm["fertilizersdata"] = _parse_fertilizer_dose_list(norm.get("fertilizersdata"))
        norm["fertilizersdatacombTwo"] = _parse_fertilizer_dose_list(norm.get("fertilizersdatacombTwo"))
        norm["recommendations"] = _parse_nf_reco_list(norm.get("recommendations"))

        og = norm.get("organicFertilizer")
        if isinstance(og, dict):
            try:
                norm["organicFertilizer"] = GfrOrganicFertilizerBlock.model_validate(og)
            except ValidationError:
                norm["organicFertilizer"] = None
        else:
            norm["organicFertilizer"] = None

        return norm

    def __str__(self) -> str:
        crop = self.crop or "Crop"
        out: List[str] = [f"- Crop - {crop}"]
        hints: List[str] = []
        rest = str(crop)
        while True:
            a = rest.find("(")
            if a == -1:
                break
            b = rest.find(")", a + 1)
            if b == -1:
                break
            inner = rest[a + 1 : b].strip()
            if inner:
                hints.append(inner)
            rest = rest[b + 1 :]

        trivial = {"", "all", "all variety", "all varieties"}
        uniq_hints: List[str] = []
        seen_l: set[str] = set()
        for h in hints:
            hl = h.lower().strip()
            if hl in trivial or hl in seen_l:
                continue
            seen_l.add(hl)
            uniq_hints.append(h)
        if len(uniq_hints) == 1:
            out.append(f"  - When and how (from advisory label) - {uniq_hints[0]}")
        elif len(uniq_hints) > 1:
            out.append("  - When and how (from advisory label)")
            for h in uniq_hints:
                out.append(f"    - {h}")

        has_dose = False

        def _fmt_ferts(section: str, ferts: Optional[List[GfrFertilizerDoseItem]]) -> None:
            nonlocal has_dose
            if not ferts:
                return
            out.append(f"  - {section} (dosage per hectare)")
            has_dose = True
            for f in ferts:
                amt = f.amount()
                unit = f.unit or ""
                out.append(f"    - {f.display_name()} - {amt} {unit}".rstrip())

        _fmt_ferts("Recommended mix — option 1", self.fertilizersdata)
        _fmt_ferts("Recommended mix — option 2", self.fertilizersdatacombTwo)

        recs = self.recommendations
        if recs:
            out.append("  - Natural farming — practice advisory (from network)")
            has_dose = True
            for r in recs:
                comp = (r.component or "").strip()
                text = (r.recommendation or "").strip()
                purpose = (r.purpose or "").strip()
                if not text and not comp:
                    continue
                head = f"{comp}: {text}" if comp else text
                if purpose:
                    head = f"{head} — {purpose}"
                out.append(f"    - {head}")

        organic = self.organicFertilizer
        if organic is not None:
            wrote_org = False
            for attr, label in [
                ("fym", "FYM"),
                ("compost", "Compost"),
                ("vermicompost", "Vermicompost"),
                ("oilCake", "Oil cake"),
                ("bioFertilizers", "Biofertilizers"),
                ("method", "Method"),
            ]:
                v = getattr(organic, attr, None)
                if v in (None, "", 0, "0", "0.0"):
                    continue
                if not wrote_org:
                    out.append("  - Natural farming — organic inputs (quantities per hectare where given)")
                    wrote_org = True
                    has_dose = True
                unit_attr = f"{attr}Unit"
                unit = getattr(organic, unit_attr, None) or ""
                out.append(f"    - {label} - {v} {unit}".rstrip())

        if not has_dose:
            out.append(
                "  - (No fertilizer or organic doses in this advisory segment — "
                "network may have returned crop label only; check SHC / phone / cycle or another crop row.)"
            )
        return "\n".join(out)


class GfrRecommendationItem(BaseModel):
    """One catalog item from recommendation `items[]` (tags with embedded JSON)."""

    model_config = ConfigDict(extra="ignore")
    id: Optional[str] = None
    descriptor: Optional[GfrDescriptor] = None
    tags: Optional[List[Dict[str, Any]]] = None

    def format_block(self) -> str:
        title = (self.descriptor.name if self.descriptor and self.descriptor.name else None) or self.id or "Recommendation"
        _redundant = title.strip().casefold() in {
            "crop recommendation",
            "recommendation",
            "gfr-recommendation",
        }
        lines: List[str] = [] if _redundant else [f"\n{title}"]
        tags = self.tags or []
        parsed_any = False

        rec_tag = next(
            (
                t
                for t in tags
                if isinstance(t, dict)
                and isinstance(t.get("descriptor"), dict)
                and (t["descriptor"].get("code") == "recommendations")
            ),
            None,
        )
        if isinstance(rec_tag, dict) and isinstance(rec_tag.get("list"), list):
            data_item = next(
                (
                    li
                    for li in rec_tag["list"]
                    if isinstance(li, dict)
                    and isinstance(li.get("descriptor"), dict)
                    and (li["descriptor"].get("code") == "data")
                ),
                None,
            )
            raw_val = data_item.get("value") if isinstance(data_item, dict) else None
            payload: Any = None
            if isinstance(raw_val, str) and raw_val.strip():
                try:
                    payload = json.loads(raw_val)
                except json.JSONDecodeError:
                    payload = None
            elif isinstance(raw_val, list):
                payload = raw_val
            if isinstance(payload, list) and payload:
                for entry in payload[:6]:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        lines.extend(str(GfrRecoPayloadRow.model_validate(entry)).splitlines())
                        parsed_any = True
                    except ValidationError:
                        continue

        if not parsed_any:
            for t in tags:
                if not isinstance(t, dict):
                    continue
                t_list = t.get("list")
                if not isinstance(t_list, list):
                    continue
                for ti in t_list:
                    if not isinstance(ti, dict):
                        continue
                    desc = ti.get("descriptor") or {}
                    name = desc.get("name") or desc.get("code") or "Info"
                    val = ti.get("value")
                    if val is None:
                        continue
                    lines.append(f"- {name} - {val}")

        if not tags:
            lines.append(f"- id - {self.id}")
        return "\n".join(lines).strip()


def gfr_get_crop_registries(
    ctx: RunContext[FarmerContext],
    latitude: float,
    longitude: float,
    only_gfr_available: bool = True,
    crop_name_contains: Optional[str] = None,
    limit: int = 25,
) -> str:
    """Crop registry from BAP /search (location lat/lon)."""
    if limit < 1:
        limit = 1
    if limit > 50:
        limit = 50

    payload = GfrCropRegistrySearch(
        latitude=latitude,
        longitude=longitude,
        session_id=ctx.deps.session_id,
        question_id=ctx.deps.question_id,
    ).get_payload()
    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        raise ModelRetry("BAP_ENDPOINT is not configured for GFR network calls.")
    search_url = bap_endpoint.rstrip("/") + "/search"
    try:
        response = httpx.post(
            search_url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=get_default_httpx_timeout(),
        )
    except httpx.TimeoutException:
        raise ModelRetry("GFR network request timed out. Please try again.")
    except httpx.RequestError as e:
        raise ModelRetry(f"GFR network request failed. {str(e)}")
    if response.status_code != 200:
        logger.error(
            "GFR Beckn search HTTP %s — %s",
            response.status_code,
            (response.text[:500] if response.text else "(empty)"),
        )
        raise ModelRetry("GFR service unavailable right now. Please try again.")
    try:
        raw = response.json()
    except Exception:
        raise ModelRetry("GFR service returned an invalid response.")

    try:
        parsed = GfrClientSearchResponse.model_validate(raw)
    except ValidationError as e:
        logger.warning("GFR registry response validation failed: %s", e)
        raise ModelRetry("GFR service returned an unexpected response format.")

    crops: List[GfrRegistryItem] = []
    for x in parsed.iter_raw_items():
        try:
            crops.append(GfrRegistryItem.model_validate(x))
        except ValidationError:
            continue

    if not crops:
        return "No crops found."

    needle = (crop_name_contains or "").strip().lower()

    filtered: List[GfrRegistryItem] = []
    for c in crops:
        if only_gfr_available and c.gfr_available is False:
            continue
        name = (c.combined_name or c.crop_name or "").lower()
        if needle and needle not in name:
            continue
        filtered.append(c)

    if not filtered:
        msg = "No crops matched your filter."
        if only_gfr_available:
            msg += " (Only showing crops where recommendations are available.)"
        return msg

    lines = ["Crops (id - name - stateId - districtId - season - irrigation - GFR):"]
    for c in filtered[:limit]:
        cid = c.id or ""
        cname = c.combined_name or c.crop_name or ""
        sid = c.state_id or ""
        did = c.district_id or ""
        season = c.season or ""
        irrig = c.irrigation_type or ""
        gfr_ok = c.gfr_available
        gfr_txt = "Yes" if gfr_ok is True else ("No" if gfr_ok is False else "Unknown")
        if cid and cname:
            lines.append(
                f"- {cid} - {cname} - {sid} - {did} - {season} - {irrig} - {gfr_txt}"
            )

    return "\n".join(lines).strip()


def gfr_get_recommendations(
    ctx: RunContext[FarmerContext],
    state_id: str,
    crops: List[str],
    phone_no: str,
    cycle: str,
    district_id: Optional[str] = None,
    natural_farming: bool = False,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
) -> str:
    """
    Fertilizer recommendation from BAP /search. Pass stateId, crop ids (from registry), phone
    (10-digit Indian number or with 91 / +91), SHC cycle year; optional
    districtId and lat/lon. Soil values come from the network (SHC). Default natural_farming=False
    matches sandbox /search (chemical + organic quantity tables); True returns NF practice advisory text.
    """
    if not state_id or len(state_id.strip()) < 6:
        raise ModelRetry("Please provide a valid state ID.")
    if not crops:
        raise ModelRetry("Please provide at least one crop ID.")
    crop_ids = [c.strip() for c in crops if c and c.strip()]
    if not crop_ids:
        raise ModelRetry("Please provide valid crop IDs.")
    if len(crop_ids) > 6:
        crop_ids = crop_ids[:6]

    ten_digit = normalize_phone_for_api(to_ascii_digits(phone_no or ""))
    if len(ten_digit) != 10 or not ten_digit.isdigit():
        raise ModelRetry(
            "Please provide a valid Indian mobile number registered on the Soil Health Card "
            "(10 digits, or with country code 91 / +91)."
        )
    phone = f"+91{ten_digit}"
    cycle_y = (cycle or "").strip()
    if not cycle_y:
        raise ModelRetry("Please provide SHC cycle year (e.g. 2025-26).")

    payload = GfrRecommendationSearch(
        state_id=state_id.strip(),
        crops=crop_ids,
        phone_no=phone,
        cycle=cycle_y,
        district_id=district_id.strip() if district_id else None,
        natural_farming=natural_farming,
        latitude=latitude,
        longitude=longitude,
        session_id=ctx.deps.session_id,
        question_id=ctx.deps.question_id,
    ).get_payload()

    bap_endpoint = os.getenv("BAP_ENDPOINT")
    if not bap_endpoint:
        raise ModelRetry("BAP_ENDPOINT is not configured for GFR network calls.")
    search_url = bap_endpoint.rstrip("/") + "/search"
    try:
        response = httpx.post(
            search_url,
            json=payload,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            timeout=get_default_httpx_timeout(),
        )
    except httpx.TimeoutException:
        raise ModelRetry("GFR network request timed out. Please try again.")
    except httpx.RequestError as e:
        raise ModelRetry(f"GFR network request failed. {str(e)}")
    if response.status_code != 200:
        logger.error(
            "GFR Beckn search HTTP %s — %s",
            response.status_code,
            (response.text[:500] if response.text else "(empty)"),
        )
        raise ModelRetry("GFR service unavailable right now. Please try again.")
    try:
        raw = response.json()
    except Exception:
        raise ModelRetry("GFR service returned an invalid response.")

    try:
        parsed = GfrClientSearchResponse.model_validate(raw)
    except ValidationError as e:
        logger.warning("GFR recommendation response validation failed: %s", e)
        raise ModelRetry("GFR service returned an unexpected response format.")

    raw_items = parsed.iter_raw_items()
    if not raw_items:
        return "No recommendation data found."

    blocks: List[str] = []
    for item_dict in raw_items[:6]:
        try:
            blocks.append(GfrRecommendationItem.model_validate(item_dict).format_block())
        except ValidationError:
            continue
    if not blocks:
        return "No recommendation data found."
    return "\n".join(["Fertilizer recommendation:", *blocks]).strip()
