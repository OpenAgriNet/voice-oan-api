"""
Structured farmer profile — the stable, high-value layer of long-term memory.

Where mem0/Qdrant holds free-form *episodic* memory (past conversations, advice),
this module holds a small set of **structured** facts that change rarely and are
worth loading deterministically at the start of every call:

    crop(s), land, irrigation, soil, location, preferred mandi, language,
    livestock, schemes, and open follow-up threads.

Storage: one point per hashed user_id in a dedicated Qdrant collection
(separate from mem0's memory collection). Looked up by id only — no embeddings,
no vector search — so durable profile data stays out of Redis memory.

Lifecycle:
  load + render_snapshot   — call start, injected into the system prompt
  apply_update / forget    — mid-call, via the update_farmer_profile tool
  extract_and_merge        — post-call, structured extraction from the transcript
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from datetime import date, datetime, timezone
from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

logger = logging.getLogger(__name__)

# Profiles live in their own Qdrant collection (NOT in Redis, and NOT in mem0's
# memory collection). We only ever point-lookup by user_id, so each profile is
# one Qdrant point with a deterministic UUID id and a placeholder vector — no
# embeddings, no similarity search. The structured profile sits in the payload.
_PROFILE_COLLECTION = os.getenv("QDRANT_PROFILE_COLLECTION", "vistaar_farmer_profiles")
_PROFILE_ID_NAMESPACE = uuid.UUID("6f9619ff-8b86-d011-b42d-00c04fc964ff")  # fixed seed
_PLACEHOLDER_VECTOR = [0.0]  # 1-dim; never used for search, only to satisfy Qdrant


def snapshot_cache_key(user_id: str) -> str:
    """Redis key for the rendered call-start snapshot (see stream_voice_message).

    Cached so a new call doesn't pay the Qdrant + mem0 (OpenAI embedding) round
    trip before the agent can start. Invalidated on profile save/delete.
    """
    return f"profile_snapshot_{user_id}"


async def _invalidate_snapshot_cache(user_id: str) -> None:
    try:
        from app.core.cache import cache

        await cache.delete(snapshot_cache_key(user_id))
    except Exception:
        logger.debug("snapshot cache invalidation failed for user %s", user_id, exc_info=True)


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
class Crop(BaseModel):
    name: str = Field(description="Crop name, e.g. cotton, wheat, soybean.")
    variety: Optional[str] = Field(default=None, description="Seed variety/hybrid if known.")
    area_acres: Optional[float] = Field(default=None, description="Area under this crop, in acres.")
    sowing_date: Optional[str] = Field(default=None, description="ISO date (YYYY-MM-DD) of sowing.")
    season: Optional[str] = Field(default=None, description="kharif | rabi | zaid")


class OpenThread(BaseModel):
    """An unresolved topic to follow up on at the next call (proactive UX)."""
    topic: str = Field(description="What it was about, e.g. 'bollworm on cotton'.")
    advice_given: Optional[str] = Field(default=None, description="Advice given last time, if any.")
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    status: str = Field(default="open", description="open | resolved")


class FarmerProfile(BaseModel):
    user_id: str
    name: Optional[str] = None

    # Location
    village: Optional[str] = None
    district: Optional[str] = None
    state: Optional[str] = None
    preferred_mandi: Optional[str] = None

    # Farm
    crops: list[Crop] = Field(default_factory=list)
    land_area_acres: Optional[float] = None
    irrigation: Optional[str] = Field(default=None, description="rainfed | borewell | canal | drip | sprinkler")
    soil_type: Optional[str] = None
    livestock: list[str] = Field(default_factory=list)

    # Engagement
    language: Optional[str] = Field(default=None, description="Preferred language/dialect.")
    preferred_call_time: Optional[str] = None
    schemes: list[str] = Field(default_factory=list, description="Schemes discussed or enrolled.")
    open_threads: list[OpenThread] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list, description="Misc stable facts not covered above.")

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def is_empty(self) -> bool:
        return not any([
            self.name, self.village, self.district, self.preferred_mandi,
            self.crops, self.land_area_acres, self.irrigation, self.soil_type,
            self.livestock, self.schemes, self.open_threads, self.notes,
        ])


# --------------------------------------------------------------------------- #
# Crop-stage estimation (turns a sowing date into actionable context)
# --------------------------------------------------------------------------- #
# Approximate total crop duration (days) for common Indian crops. Used only to
# label a coarse growth stage; precise agronomy is the agent's job, not ours.
_CROP_DURATION_DAYS = {
    "cotton": 170, "wheat": 130, "rice": 120, "paddy": 120, "soybean": 100,
    "maize": 110, "sugarcane": 330, "gram": 110, "chana": 110, "tur": 160,
    "groundnut": 120, "onion": 120, "tomato": 100, "potato": 100, "mustard": 120,
}


def _crop_age_state(crop: Crop) -> str:
    """Classify a crop by how far past its season the sowing date is.

    'current'  — in season (or age unknown): show with computed stage.
    'stale'    — past harvest but recent: show as last season's crop so the
                 agent asks instead of assuming ("कापूस अजूनही घेता का?").
    'expired'  — long gone (>2 seasons/a year): drop from the snapshot; the
                 record stays in Qdrant, we just stop presenting it as fact.

    Without this, a crop sown last June reads "near harvest" forever and can
    color advice with a crop the farmer no longer grows.
    """
    if not crop.sowing_date:
        return "current"
    try:
        sown = date.fromisoformat(crop.sowing_date)
    except (ValueError, TypeError):
        return "current"
    days = (date.today() - sown).days
    if days < 0:
        return "current"
    total = _CROP_DURATION_DAYS.get(crop.name.strip().lower(), 130)
    if days <= int(total * 1.3):
        return "current"
    if days <= max(2 * total, 365):
        return "stale"
    return "expired"


def _crop_stage(crop: Crop) -> Optional[str]:
    """Return a short 'day N, ~flowering stage' label, or None if no sowing date."""
    if not crop.sowing_date:
        return None
    try:
        sown = date.fromisoformat(crop.sowing_date)
    except (ValueError, TypeError):
        return None
    days = (date.today() - sown).days
    if days < 0:
        return None
    total = _CROP_DURATION_DAYS.get(crop.name.strip().lower())
    if total:
        frac = days / total
        if frac < 0.12:
            stage = "germination"
        elif frac < 0.4:
            stage = "vegetative"
        elif frac < 0.7:
            stage = "flowering"
        elif frac < 0.95:
            stage = "maturing"
        else:
            stage = "near harvest"
    else:
        stage = "vegetative" if days < 45 else "flowering" if days < 90 else "maturing"
    return f"day {days}, ~{stage}"


# --------------------------------------------------------------------------- #
# District GPS (Maharashtra district HQs)
# --------------------------------------------------------------------------- #
# Putting coordinates in the snapshot lets the model call weather/mandi tools
# directly instead of spending a whole LLM round on forward_geocode first.
_MH_DISTRICT_GPS: dict[str, tuple[float, float]] = {
    "ahmednagar": (19.0948, 74.7480), "अहमदनगर": (19.0948, 74.7480),
    "akola": (20.7002, 77.0082), "अकोला": (20.7002, 77.0082),
    "amravati": (20.9374, 77.7796), "अमरावती": (20.9374, 77.7796),
    "aurangabad": (19.8762, 75.3433), "औरंगाबाद": (19.8762, 75.3433),
    "chhatrapati sambhajinagar": (19.8762, 75.3433), "छत्रपती संभाजीनगर": (19.8762, 75.3433),
    "beed": (18.9891, 75.7601), "बीड": (18.9891, 75.7601),
    "bhandara": (21.1704, 79.6522), "भंडारा": (21.1704, 79.6522),
    "buldhana": (20.5293, 76.1842), "बुलढाणा": (20.5293, 76.1842),
    "chandrapur": (19.9615, 79.2961), "चंद्रपूर": (19.9615, 79.2961),
    "dhule": (20.9042, 74.7749), "धुळे": (20.9042, 74.7749),
    "gadchiroli": (20.1809, 80.0000), "गडचिरोली": (20.1809, 80.0000),
    "gondia": (21.4602, 80.1920), "गोंदिया": (21.4602, 80.1920),
    "hingoli": (19.7173, 77.1494), "हिंगोली": (19.7173, 77.1494),
    "jalgaon": (21.0077, 75.5626), "जळगाव": (21.0077, 75.5626),
    "jalna": (19.8410, 75.8864), "जालना": (19.8410, 75.8864),
    "kolhapur": (16.7050, 74.2433), "कोल्हापूर": (16.7050, 74.2433),
    "latur": (18.4088, 76.5604), "लातूर": (18.4088, 76.5604),
    "mumbai": (19.0760, 72.8777), "मुंबई": (19.0760, 72.8777),
    "nagpur": (21.1458, 79.0882), "नागपूर": (21.1458, 79.0882),
    "nanded": (19.1383, 77.3210), "नांदेड": (19.1383, 77.3210),
    "nandurbar": (21.3697, 74.2400), "नंदुरबार": (21.3697, 74.2400),
    "nashik": (19.9975, 73.7898), "नाशिक": (19.9975, 73.7898),
    "osmanabad": (18.1860, 76.0419), "उस्मानाबाद": (18.1860, 76.0419),
    "dharashiv": (18.1860, 76.0419), "धाराशिव": (18.1860, 76.0419),
    "palghar": (19.6967, 72.7699), "पालघर": (19.6967, 72.7699),
    "parbhani": (19.2686, 76.7708), "परभणी": (19.2686, 76.7708),
    "pune": (18.5204, 73.8567), "पुणे": (18.5204, 73.8567),
    "raigad": (18.6414, 72.8722), "रायगड": (18.6414, 72.8722),
    "ratnagiri": (16.9902, 73.3120), "रत्नागिरी": (16.9902, 73.3120),
    "sangli": (16.8524, 74.5815), "सांगली": (16.8524, 74.5815),
    "satara": (17.6805, 74.0183), "सातारा": (17.6805, 74.0183),
    "sindhudurg": (16.1200, 73.6900), "सिंधुदुर्ग": (16.1200, 73.6900),
    "solapur": (17.6599, 75.9064), "सोलापूर": (17.6599, 75.9064),
    "thane": (19.2183, 72.9781), "ठाणे": (19.2183, 72.9781),
    "wardha": (20.7453, 78.6022), "वर्धा": (20.7453, 78.6022),
    "washim": (20.1110, 77.1330), "वाशिम": (20.1110, 77.1330),
    "yavatmal": (20.3888, 78.1204), "यवतमाळ": (20.3888, 78.1204),
}


def _district_gps(name: Optional[str]) -> Optional[tuple[float, float]]:
    if not name:
        return None
    return _MH_DISTRICT_GPS.get(name.strip().lower())


# --------------------------------------------------------------------------- #
# Snapshot rendering (call-start system-prompt injection)
# --------------------------------------------------------------------------- #
def render_snapshot(profile: FarmerProfile) -> Optional[str]:
    """Render the structured profile as a compact bullet block for the system prompt.

    Includes computed crop-stage and any open follow-up threads, which is what
    lets the agent open with a personalized, proactive greeting.
    """
    if profile.is_empty():
        return None

    lines: list[str] = ["Farmer profile (remember this; greet them by referring to it naturally):"]

    loc = ", ".join(x for x in [profile.village, profile.district, profile.state] if x)
    if profile.name:
        lines.append(f"• Name: {profile.name}")
    if loc:
        lines.append(f"• Location: {loc}")
    gps = _district_gps(profile.district) or _district_gps(profile.village)
    if gps:
        lines.append(
            f"• GPS: latitude={gps[0]}, longitude={gps[1]} "
            "(pass directly to weather/mandi tools; no geocoding needed)"
        )
    if profile.preferred_mandi:
        lines.append(f"• Preferred mandi: {profile.preferred_mandi}")

    for crop in profile.crops:
        age_state = _crop_age_state(crop)
        if age_state == "expired":
            continue
        parts = [crop.name]
        if crop.variety:
            parts.append(f"({crop.variety})")
        if crop.area_acres:
            parts.append(f"{crop.area_acres} acre")
        if age_state == "stale":
            parts.append("— sown LAST season; ask if they still grow it, do not assume")
        else:
            stage = _crop_stage(crop)
            if stage:
                parts.append(f"— {stage}")
        lines.append(f"• Crop: {' '.join(parts)}")

    if profile.land_area_acres:
        lines.append(f"• Total land: {profile.land_area_acres} acre")
    if profile.irrigation:
        lines.append(f"• Irrigation: {profile.irrigation}")
    if profile.soil_type:
        lines.append(f"• Soil: {profile.soil_type}")
    if profile.livestock:
        lines.append(f"• Livestock: {', '.join(profile.livestock)}")
    if profile.schemes:
        lines.append(f"• Schemes: {', '.join(profile.schemes)}")
    for note in profile.notes[:3]:
        lines.append(f"• Note: {note}")

    open_threads = [t for t in profile.open_threads if t.status == "open"]
    if open_threads:
        lines.append("Follow up on (ask how it went):")
        for t in open_threads[:3]:
            suffix = f" (you advised: {t.advice_given})" if t.advice_given else ""
            lines.append(f"• {t.topic}{suffix}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Merge logic
# --------------------------------------------------------------------------- #
_SCALAR_FIELDS = [
    "name", "village", "district", "state", "preferred_mandi",
    "land_area_acres", "irrigation", "soil_type", "language", "preferred_call_time",
]
_LIST_FIELDS = ["livestock", "schemes", "notes"]


def merge_profile(existing: FarmerProfile, partial: dict) -> FarmerProfile:
    """Deep-merge a partial update into an existing profile (non-destructive).

    Scalars overwrite when present & non-null. Lists union (case-insensitive).
    Crops merge by name. Open threads append. Empty/null values never erase data.
    """
    data = existing.model_dump()

    for field in _SCALAR_FIELDS:
        val = partial.get(field)
        if val not in (None, "", []):
            data[field] = val

    for field in _LIST_FIELDS:
        incoming = partial.get(field) or []
        if isinstance(incoming, str):
            incoming = [incoming]
        seen = {str(x).strip().lower() for x in data.get(field, [])}
        for item in incoming:
            if item and str(item).strip().lower() not in seen:
                data[field].append(item)
                seen.add(str(item).strip().lower())

    # Crops: merge by lowercased name.
    incoming_crops = partial.get("crops") or []
    by_name = {c["name"].strip().lower(): c for c in data.get("crops", []) if c.get("name")}
    for raw in incoming_crops:
        crop = raw if isinstance(raw, dict) else {"name": str(raw)}
        name = (crop.get("name") or "").strip().lower()
        if not name:
            continue
        if name in by_name:
            for k, v in crop.items():
                if v not in (None, "", []):
                    by_name[name][k] = v
        else:
            by_name[name] = crop
    data["crops"] = list(by_name.values())

    # Open threads: append new topics not already open.
    incoming_threads = partial.get("open_threads") or []
    open_topics = {t["topic"].strip().lower() for t in data.get("open_threads", []) if t.get("topic")}
    for raw in incoming_threads:
        thread = raw if isinstance(raw, dict) else {"topic": str(raw)}
        topic = (thread.get("topic") or "").strip().lower()
        if topic and topic not in open_topics:
            data["open_threads"].append(OpenThread(**thread).model_dump())
            open_topics.add(topic)

    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    return FarmerProfile(**data)


# --------------------------------------------------------------------------- #
# Structured extraction from a transcript
# --------------------------------------------------------------------------- #
_EXTRACTION_SYSTEM = (
    "You extract a farmer's durable profile facts from a call transcript. "
    "Return ONLY a JSON object with any of these keys you are CONFIDENT about "
    "(omit keys you are unsure of, never guess): "
    "name, village, district, state, preferred_mandi, land_area_acres (number), "
    "irrigation, soil_type, language, preferred_call_time, "
    "livestock (list of strings), schemes (list of strings), notes (list of strings), "
    "crops (list of {name, variety, area_acres, sowing_date YYYY-MM-DD, season}), "
    "open_threads (list of {topic, advice_given}) for unresolved issues to follow up on. "
    "Only include stable facts, not one-off price/weather questions. "
    "preferred_mandi ONLY if the farmer explicitly says where they sell their produce — "
    "NEVER infer it from a warehouse/godown location, a staff contact, or a price lookup "
    "the assistant performed. "
    "Return {} if nothing durable was learned. Output JSON only, no prose."
)


def _transcript_to_text(history: list[ModelMessage]) -> str:
    lines: list[str] = []
    for msg in history:
        for part in msg.parts:
            kind = getattr(part, "part_kind", "")
            if kind == "user-prompt":
                lines.append(f"Farmer: {part.content}")
            elif kind == "text":
                lines.append(f"Assistant: {part.content}")
    return "\n".join(lines)


def _extract_partial_sync(transcript: str) -> dict:
    """Blocking call to the vLLM extraction model. Returns a partial-profile dict."""
    from openai import OpenAI
    from agents.models import LLM_AGRINET_MODEL_NAME, _vllm_openai_base_url

    base_url = _vllm_openai_base_url()
    model = LLM_AGRINET_MODEL_NAME or os.getenv("LLM_MODEL_NAME") or "agrinet-model"
    if not base_url:
        return {}

    client = OpenAI(base_url=base_url, api_key=os.getenv("INFERENCE_API_KEY") or "not-required")
    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _EXTRACTION_SYSTEM},
            {"role": "user", "content": transcript},
        ],
        temperature=0.0,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content[content.find("{"):]
    start, end = content.find("{"), content.rfind("}")
    if start == -1 or end == -1:
        return {}
    try:
        return json.loads(content[start:end + 1])
    except json.JSONDecodeError:
        logger.warning("profile extraction: non-JSON response, skipping")
        return {}


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class ProfileStore:
    # After a failed init, wait this long before trying again — so a Qdrant
    # blip at first use degrades profiles for a minute, not until restart,
    # while still not hammering a down Qdrant on every call.
    _INIT_RETRY_SECONDS = 60.0

    def __init__(self) -> None:
        self._client = None
        self._last_init_failure: Optional[float] = None

    def _get_client(self):
        """Lazily create the Qdrant client and ensure the profile collection exists."""
        if self._client is not None:
            return self._client
        if (
            self._last_init_failure is not None
            and time.monotonic() - self._last_init_failure < self._INIT_RETRY_SECONDS
        ):
            return None
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.models import Distance, VectorParams
            from app.config import settings

            client = QdrantClient(
                host=getattr(settings, "qdrant_host", "localhost"),
                port=int(getattr(settings, "qdrant_port", 6333)),
            )
            existing = {c.name for c in client.get_collections().collections}
            if _PROFILE_COLLECTION not in existing:
                client.create_collection(
                    collection_name=_PROFILE_COLLECTION,
                    vectors_config=VectorParams(size=1, distance=Distance.DOT),
                )
                logger.info("ProfileStore: created Qdrant collection %s", _PROFILE_COLLECTION)
            self._client = client
            self._last_init_failure = None
            logger.info("ProfileStore: Qdrant client initialized")
        except Exception:
            self._last_init_failure = time.monotonic()
            logger.warning(
                "ProfileStore: init failed — profile disabled, retrying in %ss",
                self._INIT_RETRY_SECONDS, exc_info=True,
            )
        return self._client

    @staticmethod
    def _point_id(user_id: str) -> str:
        """Deterministic Qdrant point id (UUID) from the hashed user_id."""
        return str(uuid.uuid5(_PROFILE_ID_NAMESPACE, user_id))

    async def get(self, user_id: str) -> Optional[FarmerProfile]:
        if not user_id:
            return None
        client = self._get_client()
        if not client:
            return None
        try:
            records = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.retrieve(
                    collection_name=_PROFILE_COLLECTION,
                    ids=[self._point_id(user_id)],
                    with_payload=True,
                ),
            )
            if not records:
                return None
            return FarmerProfile(**records[0].payload)
        except Exception:
            logger.warning("profile.get failed for user %s", user_id, exc_info=True)
            return None

    async def save(self, profile: FarmerProfile) -> None:
        client = self._get_client()
        if not client:
            return
        try:
            from qdrant_client.models import PointStruct

            point = PointStruct(
                id=self._point_id(profile.user_id),
                vector=_PLACEHOLDER_VECTOR,
                payload=profile.model_dump(),
            )
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.upsert(collection_name=_PROFILE_COLLECTION, points=[point]),
            )
            await _invalidate_snapshot_cache(profile.user_id)
        except Exception:
            logger.warning("profile.save failed for user %s", profile.user_id, exc_info=True)

    async def get_snapshot(self, user_id: str) -> Optional[str]:
        """Load the profile and render the call-start snapshot, or None."""
        profile = await self.get(user_id)
        if not profile:
            return None
        return render_snapshot(profile)

    async def apply_update(self, user_id: str, partial: dict) -> FarmerProfile:
        """Merge a partial update into the stored profile and persist it."""
        existing = await self.get(user_id) or FarmerProfile(user_id=user_id)
        merged = merge_profile(existing, partial)
        await self.save(merged)
        return merged

    async def forget(self, user_id: str, field: str, value: Optional[str] = None) -> bool:
        """Remove a field (or a single list/crop entry) from the profile.

        Returns True if something changed. Used for voice-driven corrections like
        'I don't grow cotton anymore'.
        """
        profile = await self.get(user_id)
        if not profile:
            return False
        data = profile.model_dump()
        changed = False

        if field == "crops" and value:
            v = value.strip().lower()
            new = [c for c in data["crops"] if c.get("name", "").strip().lower() != v]
            changed = len(new) != len(data["crops"])
            data["crops"] = new
        elif field in _LIST_FIELDS and value:
            v = value.strip().lower()
            new = [x for x in data[field] if str(x).strip().lower() != v]
            changed = len(new) != len(data[field])
            data[field] = new
        elif field in _SCALAR_FIELDS and data.get(field) is not None:
            data[field] = None
            changed = True
        elif field == "open_threads" and value:
            v = value.strip().lower()
            for t in data["open_threads"]:
                if v in t.get("topic", "").strip().lower():
                    t["status"] = "resolved"
                    changed = True

        if changed:
            data["updated_at"] = datetime.now(timezone.utc).isoformat()
            await self.save(FarmerProfile(**data))
        return changed

    async def delete(self, user_id: str) -> bool:
        if not user_id:
            return False
        client = self._get_client()
        if not client:
            return False
        try:
            existed = await self.get(user_id) is not None
            await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: client.delete(
                    collection_name=_PROFILE_COLLECTION,
                    points_selector=[self._point_id(user_id)],
                ),
            )
            await _invalidate_snapshot_cache(user_id)
            return existed
        except Exception:
            logger.warning("profile.delete failed for user %s", user_id, exc_info=True)
            return False

    async def extract_and_merge(self, user_id: str, history: list[ModelMessage]) -> None:
        """Post-call: extract structured facts from the transcript and merge them."""
        if not user_id:
            return
        transcript = _transcript_to_text(history)
        if not transcript.strip():
            logger.info("profile.extract_and_merge: empty transcript for user %s", user_id)
            return
        try:
            partial = await asyncio.get_event_loop().run_in_executor(
                None, lambda: _extract_partial_sync(transcript)
            )
        except Exception:
            logger.error("profile extraction failed for user %s", user_id, exc_info=True)
            return
        if not partial:
            logger.info("profile.extract_and_merge: no durable facts for user %s", user_id)
            return
        # Visibility into what we're about to persist (quality gate / debugging).
        logger.info("profile.extract_and_merge user=%s extracted=%s", user_id, partial)
        merged = await self.apply_update(user_id, partial)
        logger.info(
            "profile saved for user %s (crops=%d, threads=%d)",
            user_id, len(merged.crops), len(merged.open_threads),
        )


profile_store = ProfileStore()
