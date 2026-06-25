"""
Structured farmer profile — the stable, high-value layer of long-term memory.

Where mem0/Qdrant holds free-form *episodic* memory (past conversations, advice),
this module holds a small set of **structured** facts that change rarely and are
worth loading deterministically at the start of every call:

    crop(s), land, irrigation, soil, location, preferred mandi, language,
    livestock, schemes, and open follow-up threads.

Storage: one JSON document per hashed user_id in Redis (via the shared aiocache
instance), with NO TTL so profiles persist across calls. No new dependency.

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
from datetime import date, datetime, timezone
from typing import Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

logger = logging.getLogger(__name__)

_NAMESPACE = "farmer_profile"


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
    if profile.preferred_mandi:
        lines.append(f"• Preferred mandi: {profile.preferred_mandi}")

    for crop in profile.crops:
        parts = [crop.name]
        if crop.variety:
            parts.append(f"({crop.variety})")
        if crop.area_acres:
            parts.append(f"{crop.area_acres} acre")
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
    def __init__(self) -> None:
        self._cache = None

    def _get_cache(self):
        if self._cache is None:
            from app.core.cache import cache
            self._cache = cache
        return self._cache

    async def get(self, user_id: str) -> Optional[FarmerProfile]:
        if not user_id:
            return None
        try:
            raw = await self._get_cache().get(user_id, namespace=_NAMESPACE)
            if not raw:
                return None
            return FarmerProfile(**raw)
        except Exception:
            logger.warning("profile.get failed for user %s", user_id, exc_info=True)
            return None

    async def save(self, profile: FarmerProfile) -> None:
        try:
            # ttl=None => persist with no expiry (overrides the 24h cache default).
            await self._get_cache().set(
                profile.user_id, profile.model_dump(), namespace=_NAMESPACE, ttl=None
            )
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
        try:
            existed = await self.get(user_id) is not None
            await self._get_cache().delete(user_id, namespace=_NAMESPACE)
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
