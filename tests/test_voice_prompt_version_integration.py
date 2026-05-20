"""
End-to-end voice-agent regressions for the three prompt variants
(mixed / gpt-5.1 / gemma4).

Run on staging (real model calls):
  VOICE_AGENT_E2E_INTEGRATION=1 \\
  pytest tests/test_voice_prompt_version_integration.py -q -rs -m integration

Run a subset by case_id substring:
  VOICE_AGENT_E2E_INTEGRATION=1 \\
  VOICE_AGENT_E2E_CASE_FILTER=vague,closing \\
  pytest tests/test_voice_prompt_version_integration.py -q -m integration

Skipped by default so the offline pytest run stays fast.

Total cases = 10 queries × 3 variants = 30. Do not let it grow past 30.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agents.deps import FarmerContext
from agents.models import LLM_MODEL
from agents.tools import BASE_TOOLS
from agents.voice import VOICE_PROMPT_VARIANTS
from helpers.utils import get_prompt
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings


# ---------------------------------------------------------------------------
# Integration gate
# ---------------------------------------------------------------------------

INTEGRATION_ENABLED = os.getenv("VOICE_AGENT_E2E_INTEGRATION", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

VARIANTS: tuple[str, ...] = tuple(VOICE_PROMPT_VARIANTS.keys())  # ("mixed", "gpt-5.1", "gemma4")


# ---------------------------------------------------------------------------
# Case model
# ---------------------------------------------------------------------------

class VoiceCase(BaseModel):
    model_config = ConfigDict(frozen=True)

    case_id: str = Field(min_length=1)
    query: str = Field(min_length=1)
    # Each inner tuple is an OR-group; at least one alias from each group must
    # appear in the assistant's reply.
    required_any: tuple[tuple[str, ...], ...] = ()
    # If any of these substrings appear in the reply, the case fails.
    forbidden_any: tuple[str, ...] = ()
    # Per-case overrides for universal shape checks.
    max_words: int = 90
    max_questions: int | None = None  # cap on "?" count
    must_end_with_question: bool = False
    # Proper-noun / product-name tokens that legitimately contain digits
    # (e.g. "A2") and should be exempted from the universal no-digits rule.
    allow_digit_tokens: tuple[str, ...] = ()
    # Some closing turns may be answered purely by calling
    # signal_conversation_state with no text body — accept that for those cases.
    allow_empty_reply: bool = False
    notes: str = ""


# ---------------------------------------------------------------------------
# Case catalog — exactly 10 cases. Multiplied by 3 variants = 30 tests.
# ---------------------------------------------------------------------------

CASES: tuple[VoiceCase, ...] = (
    VoiceCase(
        case_id="greeting",
        query="hello",
        required_any=(("hello", "hi", "namaste"),),
        max_words=30,
        notes="Bare greeting; should respond conversationally without tool calls.",
    ),
    VoiceCase(
        case_id="vague-cow-milk",
        query="My cow is not giving milk",
        forbidden_any=("because", "due to"),
        max_words=15,
        max_questions=1,
        must_end_with_question=True,
        notes="Vague-query rule: exactly one short clarification question, no explanation.",
    ),
    VoiceCase(
        case_id="persona-name",
        query="What is your name?",
        # Accept either the literal "Amul AI" or the TTS-expanded "Amul A I"
        # form the per-variant prompts instruct the model to emit.
        required_any=(("Sarlaben",), ("Amul AI", "Amul A I")),
        max_words=40,
        notes="Persona answer must name the assistant and the service.",
    ),
    VoiceCase(
        case_id="comparison-a2",
        query="What is the difference between A2 milk and normal milk?",
        required_any=(("A2", "A 2", "a two"), ("casein", "protein", "beta")),
        forbidden_any=("here are the points", "let me explain", "to summarize"),
        max_words=80,
        # "A2" is a milk-type proper noun; the downstream TTS handles it.
        # Exempt this single token from the universal no-digits rule.
        allow_digit_tokens=("A2", "a2"),
        notes="Comparison must give the main difference compactly, no article expansion.",
    ),
    VoiceCase(
        case_id="safety-fever",
        query="My cow has fever",
        required_any=(("vet", "veterinarian", "veterinary"),),
        max_words=60,
        notes="Safety-critical: must recommend a veterinarian in the same short reply.",
    ),
    VoiceCase(
        case_id="ambiguous-feed-samudri",
        query="samudri dan for buffalo",
        required_any=(("repeat", "say again", "did not understand", "didn't understand", "didn't catch", "did not catch", "clarify"),),
        forbidden_any=("seaweed", "marine", "ocean"),
        max_words=40,
        notes="Unknown feed-sounding term: must ask to repeat, never invent marine-feed advice.",
    ),
    VoiceCase(
        case_id="booking-beech-daan-need-farmer-or-species",
        query="Book beech daan for my cow",
        # Either the agent asks for a missing booking slot (technician name /
        # farmer name / species / which) OR it cleanly bails out because the
        # test session sends no Farmer Context — both are correct per the
        # AI-booking flow rules.
        required_any=(
            (
                "technician",
                "farmer",
                "which",
                "cow",
                "buffalo",
                "not available",
                "details",
                "try again",
                "later",
                "sorry",
            ),
        ),
        forbidden_any=(
            "first technician",
            "second technician",
            "third technician",
            "option one",
            "option two",
            "user id",
            "technician id",
        ),
        max_words=60,
        notes="AI booking: must ask by name, never by ordinal/option index; no tech-id leak.",
    ),
    VoiceCase(
        case_id="out-of-scope-cricket",
        query="Where is the IPL final happening?",
        forbidden_any=("mumbai", "chennai", "kolkata", "bangalore", "ahmedabad", "delhi"),
        required_any=(
            ("dairy", "livestock", "animal", "farming", "agri", "cannot help", "not able to help", "out of scope"),
        ),
        max_words=60,
        notes="Out-of-scope: must decline politely and redirect to agri/livestock.",
    ),
    VoiceCase(
        case_id="closing-no-that-is-all",
        query="No, that is all",
        required_any=(
            ("thank", "wishing", "call", "anytime", "all right", "alright", "okay"),
        ),
        max_words=80,
        # Some prompts close the call by calling signal_conversation_state with
        # no text body. Empty replies are acceptable for closing turns; the
        # tool-side signal is the actual close action.
        allow_empty_reply=True,
        notes="Closing turn: produces the friendly close-line OR signals close via the tool.",
    ),
    VoiceCase(
        case_id="pasteurization-fact",
        query="At what temperature should I boil milk?",
        required_any=(
            ("eighty five", "85"),
            ("ninety", "90"),
        ),
        forbidden_any=("one hundred", "100 degrees"),
        max_words=60,
        notes="Hardcoded fact: milk boiling temperature must be 85-90 degrees Celsius.",
    ),
)

assert len(CASES) == 10, "Cap is 10 cases × 3 variants = 30 tests; do not exceed."
EXPECTED_TEST_COUNT = len(CASES) * len(VARIANTS)
assert EXPECTED_TEST_COUNT <= 30, "Regression test count must not exceed 30."


# ---------------------------------------------------------------------------
# Case filter (for selective staging runs)
# ---------------------------------------------------------------------------

def _selected_cases() -> tuple[VoiceCase, ...]:
    raw = os.getenv("VOICE_AGENT_E2E_CASE_FILTER", "").strip()
    if not raw:
        return CASES
    tokens = [t.strip().lower() for t in re.split(r"[,\s]+", raw) if t.strip()]
    selected = tuple(c for c in CASES if any(t in c.case_id.lower() for t in tokens))
    if not selected:
        pytest.fail(
            f"VOICE_AGENT_E2E_CASE_FILTER did not match any case_id: {raw!r}",
            pytrace=False,
        )
    return selected


# ---------------------------------------------------------------------------
# Shape-check helpers
# ---------------------------------------------------------------------------

FORBIDDEN_OPENERS = (
    "please wait",
    "let me check",
    "let me see",
    "i am checking",
    "i'm checking",
    "great question",
    "here is what",
    "here are the points",
    "to answer your question",
)

FORBIDDEN_MARKDOWN_CHARS = ("*", "#", "`", "[", "]", "{", "}", "|")

FORBIDDEN_PUNCTUATION = (":", ";", "/", "–", "—")

FILLER_PLACEHOLDERS = ("--", "–", " - kilograms", " - liters")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").lower()
    return text.strip()


def _contains_any(text: str, needles: tuple[str, ...]) -> str | None:
    lowered = text.lower()
    for needle in needles:
        if needle.lower() in lowered:
            return needle
    return None


def _validate_universal_shape(reply: str, case: VoiceCase) -> list[str]:
    issues: list[str] = []
    if not reply or not reply.strip():
        if case.allow_empty_reply:
            # Closing-style turns may legitimately respond by calling the
            # conversation-state tool with no text body.
            return issues
        issues.append("empty reply")
        return issues

    lowered = reply.lower().strip()

    # Word cap
    word_count = len(reply.split())
    if word_count > case.max_words:
        issues.append(f"word_count={word_count} exceeds max_words={case.max_words}")

    # Forbidden openers
    for opener in FORBIDDEN_OPENERS:
        if lowered.startswith(opener):
            issues.append(f"forbidden opener: {opener!r}")

    # Digit characters (numbers should be spelled out). Whitelisted proper-noun
    # tokens like "A2" are stripped before the check.
    digit_check_text = reply
    for token in case.allow_digit_tokens:
        digit_check_text = digit_check_text.replace(token, "")
    if re.search(r"\d", digit_check_text):
        issues.append(f"contains digit characters: {reply!r}")

    # Markdown / structural chars
    for ch in FORBIDDEN_MARKDOWN_CHARS:
        if ch in reply:
            issues.append(f"contains forbidden markdown char {ch!r}")

    # Forbidden punctuation
    for ch in FORBIDDEN_PUNCTUATION:
        if ch in reply:
            issues.append(f"contains forbidden punctuation {ch!r}")

    # Placeholder dashes for missing values
    for ph in FILLER_PLACEHOLDERS:
        if ph in reply:
            issues.append(f"contains placeholder {ph!r}")

    # Question count cap
    if case.max_questions is not None and reply.count("?") > case.max_questions:
        issues.append(f"question_count={reply.count('?')} exceeds max_questions={case.max_questions}")

    if case.must_end_with_question and not reply.strip().endswith("?"):
        issues.append("reply does not end with '?'")

    # Per-case required groups (at least one alias per group)
    for group in case.required_any:
        if _contains_any(reply, group) is None:
            issues.append(f"missing required_any group {group!r}")

    # Per-case forbidden phrases
    for forbidden in case.forbidden_any:
        if forbidden.lower() in lowered:
            issues.append(f"forbidden phrase present: {forbidden!r}")

    return issues


def _failure_payload(variant: str, case: VoiceCase, reply: str, issues: list[str]) -> str:
    return json.dumps(
        {
            "variant": variant,
            "case_id": case.case_id,
            "query": case.query,
            "reply": reply,
            "issues": issues,
            "notes": case.notes,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


# ---------------------------------------------------------------------------
# Agent factory — build a one-shot agent against the variant's prompt
# ---------------------------------------------------------------------------

def _build_agent_for_variant(variant: str) -> Agent:
    prompt_name = VOICE_PROMPT_VARIANTS[variant]
    instructions = get_prompt(prompt_name)
    return Agent(
        model=LLM_MODEL,
        name=f"VoiceAgent[{variant}]",
        instrument=False,
        output_type=str,
        deps=FarmerContext,
        retries=2,
        tools=BASE_TOOLS,
        instructions=instructions,
        end_strategy="exhaustive",
        model_settings=ModelSettings(
            max_tokens=3600,
            temperature=0.0,
            parallel_tool_calls=True,
        ),
    )


async def _run_agent(agent: Agent, query: str) -> str:
    deps = FarmerContext(
        query=query,
        lang_code="en",
        target_lang="en",
        signed_in=False,
    )
    result = await agent.run(query, deps=deps)
    output = result.output if hasattr(result, "output") else getattr(result, "data", "")
    return str(output or "")


# ---------------------------------------------------------------------------
# Catalog sanity (cheap, always-on)
# ---------------------------------------------------------------------------

def test_case_catalog_has_exactly_ten_cases():
    """Guardrail: keep total integration test count at 30 (10 cases × 3 variants)."""
    assert len(CASES) == 10
    assert len(VARIANTS) == 3
    assert EXPECTED_TEST_COUNT == 30

    seen_ids = set()
    for c in CASES:
        assert c.case_id not in seen_ids, f"Duplicate case_id: {c.case_id}"
        seen_ids.add(c.case_id)


# ---------------------------------------------------------------------------
# The 30 live regression tests
# ---------------------------------------------------------------------------

SELECTED_CASES = _selected_cases()

_PARAMS = [
    pytest.param(variant, case, id=f"{variant}-{case.case_id}")
    for variant in VARIANTS
    for case in SELECTED_CASES
]


@pytest.mark.integration
@pytest.mark.parametrize("variant, case", _PARAMS)
def test_voice_variant_end_to_end(variant: str, case: VoiceCase):
    if not INTEGRATION_ENABLED:
        pytest.skip("Set VOICE_AGENT_E2E_INTEGRATION=1 to run voice variant e2e regressions")

    agent = _build_agent_for_variant(variant)
    reply = asyncio.run(_run_agent(agent, case.query))

    issues = _validate_universal_shape(reply, case)
    if issues:
        pytest.fail(
            f"Voice variant regression failed.\n"
            f"failure_payload_json={_failure_payload(variant, case, reply, issues)}",
            pytrace=False,
        )
