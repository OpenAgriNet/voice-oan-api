"""A caller with no farmer profile is TOLD, in the turn they asked.

#286 withheld the identity-taking tools when identity is unresolved, and #282's
invariant held: the invented codes stopped. What it did not do was give the model
a sentence to say. The runtime context named what was unavailable; the model,
handed a status and no words, answered the booking request from
`search_documents` instead and the caller rang off believing a visit had been
booked.

Measured on voice-production 2026-09-26: 173 turns carried the `not_found`
context lines and 12 of them (7%) said anything to the caller about
registration. This file pins the missing half — the exact sentence, one per
state, present in the message history the model actually receives.

The three states are NOT interchangeable. Telling a registered farmer whose cold
fetch timed out that they are "not registered" is the same class of harm in
reverse, and telling a caller with no number on file to "try again shortly" is
advice that cannot work.
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

# Env shim: alias pydantic-ai 1.x ``OpenAIChatModel`` to 0.2.4 ``OpenAIModel`` so
# importing app.services.voice works under both. No model object is called.
import pydantic_ai.models.openai as _pai_openai  # noqa: E402
if not hasattr(_pai_openai, "OpenAIChatModel"):
    _pai_openai.OpenAIChatModel = _pai_openai.OpenAIModel

import asyncio  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

import app.services.voice as voice  # noqa: E402
from agents.deps import FarmerContext  # noqa: E402
from agents.models.farmer import FarmerDataEnvelope  # noqa: E402
from agents.services import farmer_identity as fi  # noqa: E402

PROMPTS = Path(__file__).resolve().parents[1] / "assets" / "prompts"
PROMPT_VARIANTS = [
    "voice_system_translation_pipeline_en",
    "voice_system_translation_pipeline_gemma4_en",
    "voice_system_translation_pipeline_gpt5_1_en",
]
NEGATIVE_STATES = [fi.NOT_FOUND, fi.UNRESOLVED, fi.ANONYMOUS]


# ── One sentence per state, and they say different things ───────────────────

def test_found_has_no_spoken_line():
    assert fi.no_profile_spoken_line(fi.FOUND) is None


@pytest.mark.parametrize("state", NEGATIVE_STATES)
def test_every_negative_state_has_a_spoken_line(state):
    line = fi.no_profile_spoken_line(state)
    assert line and line.endswith(".")
    # Spoken aloud through Gujarati output translation: one sentence, no markup.
    assert "`" not in line and "*" not in line
    assert len(line.split()) <= 30, f"{state} line is too long to speak: {line}"


def test_the_three_lines_are_distinct():
    lines = {fi.no_profile_spoken_line(s) for s in NEGATIVE_STATES}
    assert len(lines) == 3


def test_not_found_says_unregistered_and_names_the_milk_society():
    line = fi.no_profile_spoken_line(fi.NOT_FOUND)
    assert "not registered" in line
    assert "milk society" in line


def test_unresolved_never_tells_a_registered_farmer_they_do_not_exist():
    """#282 cause A: 134 sessions where the 4s cold-fetch budget cancelled the
    lookup. 48% of the affected numbers booked successfully at other times."""
    line = fi.no_profile_spoken_line(fi.UNRESOLVED)
    assert "not registered" not in line
    assert "unregistered" not in line
    assert "try again" in line


def test_anonymous_does_not_offer_a_retry_that_cannot_work():
    """No usable mobile: nothing about the next turn differs, so "try again"
    is a lie. The caller has to ring from their registered number."""
    line = fi.no_profile_spoken_line(fi.ANONYMOUS)
    assert "try again" not in line
    assert "not registered" not in line
    assert "registered mobile number" in line


# ── The line reaches the context block, not just the constant ───────────────

@pytest.mark.parametrize("state", NEGATIVE_STATES)
def test_capability_lines_quote_the_spoken_line(state):
    text = "\n".join(fi.unavailable_capability_lines(state))
    assert f'"{fi.no_profile_spoken_line(state)}"' in text


@pytest.mark.parametrize("state", NEGATIVE_STATES)
def test_capability_lines_forbid_answering_with_general_advice(state):
    """The measured substitute: 2 of the 17 AI-visit askers were given textbook
    breeding advice from search_documents and no explanation."""
    text = "\n".join(fi.unavailable_capability_lines(state))
    assert "general advice" in text


def test_found_state_still_adds_nothing():
    assert fi.unavailable_capability_lines(fi.FOUND) == []


# ── ...and into the farmer block the prompt is built from ───────────────────

@pytest.mark.parametrize("envelope,state", [
    (None, fi.UNRESOLVED),
    (FarmerDataEnvelope.not_found(source="api"), fi.NOT_FOUND),
])
def test_farmer_summary_carries_the_spoken_line(envelope, state):
    assert fi.no_profile_spoken_line(state) in voice._build_compact_farmer_summary(envelope)


def test_runtime_context_message_carries_the_spoken_line():
    """What get_runtime_context_message emits is what the model reads."""
    deps = FarmerContext(
        query="book an AI visit",
        signed_in=True,
        mobile="9876543210",
        farmer_identity=fi.NOT_FOUND,
        farmer_info="\n".join(fi.unavailable_capability_lines(fi.NOT_FOUND)),
    )
    assert fi.no_profile_spoken_line(fi.NOT_FOUND) in deps.get_runtime_context_message()


def test_runtime_context_request_carries_the_spoken_line():
    """The pre-history ModelRequest is the actual delivery vehicle."""
    deps = FarmerContext(
        query="book an AI visit",
        signed_in=True,
        mobile="9876543210",
        farmer_identity=fi.UNRESOLVED,
        farmer_info="\n".join(fi.unavailable_capability_lines(fi.UNRESOLVED)),
    )
    request = voice._build_runtime_context_request(deps)
    text = "\n".join(
        part.content for part in request.parts if isinstance(getattr(part, "content", None), str)
    )
    assert fi.no_profile_spoken_line(fi.UNRESOLVED) in text


# ── The state itself: anonymous is not unresolved ───────────────────────────

def test_anonymous_is_a_valid_deps_state():
    deps = FarmerContext(query="q", farmer_identity=fi.ANONYMOUS)
    assert fi.identity_state_for_deps(deps) == fi.ANONYMOUS
    # Still fails closed on the gate.
    assert not fi.has_usable_farmer_identity(deps)
    assert "booking" not in fi.identity_tool_groups(deps)


@pytest.mark.asyncio
@pytest.mark.parametrize("state", NEGATIVE_STATES)
async def test_identity_tools_stay_hidden_in_every_negative_state(state):
    from pydantic_ai.tools import ToolDefinition
    from types import SimpleNamespace

    deps = FarmerContext(query="q", mobile="9876543210", farmer_identity=state)
    tool_def = ToolDefinition(name="create_ai_call", description="d", parameters_json_schema={})
    result = await fi.prepare_requires_farmer_identity(SimpleNamespace(deps=deps), tool_def)
    assert result is None


# ── Call site: a turn with no usable mobile is anonymous, end to end ────────

class _FakeResponseStream:
    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream_text(self, delta: bool = True, debounce_by=None):
        async def _gen():
            for chunk in self._chunks:
                yield chunk
        return _gen()

    def new_messages(self):
        return []


@pytest.fixture
def agent_input(monkeypatch):
    """Drive stream_voice_message with every network edge stubbed, and hand back
    the message history the agent was actually called with.

    Deliberately end-to-end rather than a unit test on a helper: the failure this
    guards against is the call site seeding the wrong state, which a helper test
    cannot see. (A previous fix on this project shipped with tests that covered
    only the helper.)
    """
    from agents import voice as voice_agent_module

    captured = {"history": None}

    async def _update_message_history(session_id, messages):
        return None

    async def _check_moderation(**kwargs):
        from app.services.moderation import ModerationVerdict
        return ModerationVerdict(rejected=False, reason="stub")

    async def _non_meaningful(**kwargs):
        return None

    def _run_stream(**kwargs):
        captured["history"] = kwargs.get("message_history") or []
        return _FakeResponseStream(["ok."])

    monkeypatch.setattr(voice.settings, "enable_voice_nudges", False, raising=False)
    monkeypatch.setattr(voice.settings, "fallback_enabled", False, raising=False)
    monkeypatch.setattr(voice, "update_message_history", _update_message_history)
    monkeypatch.setattr(voice, "check_moderation", _check_moderation)
    monkeypatch.setattr(voice, "check_non_meaningful_streak", _non_meaningful)
    monkeypatch.setattr(voice, "clean_message_history_for_openai", lambda history: history)
    monkeypatch.setattr(voice, "trim_history", lambda history, **kwargs: history)
    monkeypatch.setattr(voice, "format_message_pairs", lambda history, limit=None: [])
    monkeypatch.setattr(voice_agent_module.voice_agent, "run_stream", lambda **kw: _run_stream(**kw))
    monkeypatch.setattr(
        voice_agent_module.voice_agent_signed_in, "run_stream", lambda **kw: _run_stream(**kw)
    )

    async def _collect(user_id):
        async for _ in voice.stream_voice_message(
            query="Please book an insemination visit for my cow",
            session_id="s-no-profile",
            source_lang="en",  # skips pretranslation
            target_lang="en",
            user_id=user_id,
            history=[],
            provider=None,
            process_id="proc-test",
            user_info={},
            owner=None,
            http_request=None,
            call_type="inbound",
        ):
            pass
        parts = []
        for message in captured["history"] or []:
            for part in getattr(message, "parts", []) or []:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    parts.append(content)
        return "\n".join(parts)

    return _collect


def test_call_with_no_usable_mobile_speaks_the_anonymous_line(agent_input, monkeypatch):
    monkeypatch.setattr(voice, "normalize_phone_to_mobile", lambda user_id: None)
    text = asyncio.run(agent_input("anonymous"))
    assert fi.no_profile_spoken_line(fi.ANONYMOUS) in text
    # ...and specifically NOT the retry line, which is what this state used to get.
    assert fi.no_profile_spoken_line(fi.UNRESOLVED) not in text


def test_call_whose_lookup_says_not_found_speaks_the_register_line(agent_input, monkeypatch):
    async def _not_found(mobile):
        return FarmerDataEnvelope.not_found(source="api")

    monkeypatch.setattr(voice, "normalize_phone_to_mobile", lambda user_id: "9876543210")
    monkeypatch.setattr(voice, "get_or_fetch_farmer_data", _not_found)
    text = asyncio.run(agent_input("+919876543210"))
    assert fi.no_profile_spoken_line(fi.NOT_FOUND) in text
    assert fi.no_profile_spoken_line(fi.UNRESOLVED) not in text


def test_call_whose_lookup_times_out_speaks_the_retry_line(agent_input, monkeypatch):
    """Cause A: the cache layer returns a bare None. This caller may well be
    registered, so they must never hear the not_found line."""
    async def _timed_out(mobile):
        return None

    async def _no_reread(mobile):
        return None

    monkeypatch.setattr(voice, "normalize_phone_to_mobile", lambda user_id: "9876543210")
    monkeypatch.setattr(voice, "get_or_fetch_farmer_data", _timed_out)
    monkeypatch.setattr(voice, "get_farmer_data_cached_only", _no_reread)
    text = asyncio.run(agent_input("+919876543210"))
    assert fi.no_profile_spoken_line(fi.UNRESOLVED) in text
    assert fi.no_profile_spoken_line(fi.NOT_FOUND) not in text


# ── The prompt teaches the model to relay it ────────────────────────────────
# The rule used to live only inside step 1 of the AI-booking flow, i.e. reachable
# only once the model had already committed to booking. In every measured session
# it never did — it classified the turn as a question. Same section, same place,
# in all three variants.

@pytest.mark.parametrize("variant", PROMPT_VARIANTS)
def test_prompt_has_a_top_level_no_profile_section(variant):
    text = (PROMPTS / f"{variant}.md").read_text()
    assert "Farmer Profile Unavailable" in text
    heading = next(
        line for line in text.splitlines() if line.lstrip("#").strip() == "Farmer Profile Unavailable"
    )
    assert heading.startswith("#"), "must be its own section, not a numbered booking step"


@pytest.mark.parametrize("variant", PROMPT_VARIANTS)
def test_no_profile_section_precedes_the_booking_flow(variant):
    text = (PROMPTS / f"{variant}.md").read_text()
    assert text.index("Farmer Profile Unavailable") < text.index("create_ai_call")


@pytest.mark.parametrize("variant", PROMPT_VARIANTS)
def test_prompt_forbids_the_two_measured_substitutes(variant):
    """Answering with general advice, and asking the caller for codes."""
    text = (PROMPTS / f"{variant}.md").read_text()
    section = text.split("Farmer Profile Unavailable", 1)[1].split("\n#", 1)[0]
    assert "general advice" in section
    assert "ask for codes" in section
    assert "believing a booking was placed" in section
