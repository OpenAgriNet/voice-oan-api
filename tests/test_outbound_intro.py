"""Outbound-call consent gate and the decline hangup.

Raya speaks the opening line, so the FIRST outbound turn already carries the
farmer's answer to it. These tests pin that we classify that first turn and never
speak an intro ourselves (which would give the farmer two).

Covers the three-way consent verdict and, importantly, that an uncertain verdict
never ends the call. Inbound behaviour must be untouched by all of this, so one
test drives a plain inbound turn through the same path.
"""
import os

os.environ.setdefault("OPENAI_API_KEY", "test-key")
os.environ.setdefault("LLM_MODEL_NAME", "gpt-test")

# Env shim: alias pydantic-ai 1.x ``OpenAIChatModel`` to 0.2.4 ``OpenAIModel`` so
# importing app.services.voice (-> agents, which builds a model at import) works
# under both. No model object is called — every model call here is stubbed.
import pydantic_ai.models.openai as _pai_openai  # noqa: E402
if not hasattr(_pai_openai, "OpenAIChatModel"):
    _pai_openai.OpenAIChatModel = _pai_openai.OpenAIModel

import asyncio  # noqa: E402
from datetime import date, timedelta  # noqa: E402

import pytest  # noqa: E402

from app.services import outbound as ob  # noqa: E402
from app.services.outbound_consent import (  # noqa: E402
    INTENT_AFFIRMATIVE,
    INTENT_NEGATIVE,
    INTENT_OTHER,
    _parse_verdict,
)
from helpers.utils import clean_output_by_language  # noqa: E402


# ── Pure helpers ──────────────────────────────────────────────────────────────

def test_is_outbound_defaults_to_inbound():
    assert ob.is_outbound("outbound") is True
    assert ob.is_outbound("OUTBOUND ") is True
    assert ob.is_outbound("inbound") is False
    assert ob.is_outbound(None) is False
    assert ob.normalize_call_type(None) == ob.CALL_TYPE_INBOUND


def test_milk_window_is_seven_days_inclusive():
    fromdate, todate = ob.milk_window(7)
    assert date.fromisoformat(todate) - date.fromisoformat(fromdate) == timedelta(days=6)


def test_no_intro_line_is_shipped_in_this_service():
    """Raya owns the call's opening line. If an intro constant reappears here,
    the farmer hears the question twice — once from Raya, once from us."""
    assert not hasattr(ob, "OUTBOUND_INTRO")


def test_farewell_keeps_the_helpline_number():
    """Gujarati digits are normalized to words for TTS — the number must survive
    that normalization, not be filtered away."""
    spoken = clean_output_by_language(ob.OUTBOUND_DECLINE_FAREWELL["gu"], "gu")
    assert "શૂન્ય" in spoken  # "zero" — the leading 0 of 080-35453545
    assert "વૉટ્સએપ" in spoken


# ── Consent verdict parsing ───────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw,expected",
    [
        ('{"intent": "affirmative", "reason": "clear yes"}', INTENT_AFFIRMATIVE),
        ('{"intent": "negative", "reason": "declines"}', INTENT_NEGATIVE),
        ('{"intent": "other", "reason": "asks own question"}', INTENT_OTHER),
        ('{"intent": "AFFIRMATIVE", "reason": "case"}', INTENT_AFFIRMATIVE),
    ],
)
def test_parse_consent_valid(raw, expected):
    verdict = _parse_verdict(raw)
    assert verdict.intent == expected
    assert verdict.failed_open is False


@pytest.mark.parametrize("raw", ["not-json", "[]", "", '{"intent": "maybe"}', '{"reason": "x"}'])
def test_parse_consent_bad_output_falls_back_to_other(raw):
    """Never negative on a bad parse: an uncertain verdict must not hang up."""
    verdict = _parse_verdict(raw)
    assert verdict.intent == INTENT_OTHER
    assert verdict.is_negative is False
    assert verdict.failed_open is True


def test_classify_consent_empty_reply_is_other():
    from app.services.outbound_consent import classify_consent

    verdict = asyncio.run(classify_consent(reply="   ", source_lang="gu"))
    assert verdict.intent == INTENT_OTHER
    assert verdict.failed_open is False


# ── Streaming flow ────────────────────────────────────────────────────────────

class _FakeResponseStream:
    """Minimal stand-in for pydantic-ai's run_stream context manager."""

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
def voice_harness(monkeypatch):
    """Drive stream_voice_message with every network edge stubbed out."""
    from agents import voice as voice_agent_module
    from app.services import voice as voice_module

    state = {
        "history": {},
        "stage": {},
        "milk": None,
        "consent": None,
        "agent_inputs": [],
        "prefetch_calls": [],
    }

    async def _update_message_history(session_id, messages):
        state["history"][session_id] = messages

    async def _set_stage(session_id, stage):
        state["stage"][session_id] = stage

    async def _get_stage(session_id):
        return state["stage"].get(session_id)

    async def _get_prefetched(session_id):
        return state["milk"]

    async def _classify_consent(reply, source_lang):
        state["consent_reply"] = reply
        return state["consent"]

    async def _check_moderation(**kwargs):
        from app.services.moderation import ModerationVerdict
        return ModerationVerdict(rejected=False, reason="stub")

    def _spawn(coro, *, label):
        state["prefetch_calls"].append(label)
        coro.close()  # never actually hit the milk API in tests

    def _run_stream(**kwargs):
        state["agent_inputs"].append(kwargs.get("message_history") or [])
        return _FakeResponseStream(["Your milk details."])

    monkeypatch.setattr(voice_module.settings, "outbound_intro_enabled", True, raising=False)
    monkeypatch.setattr(voice_module.settings, "enable_voice_nudges", False, raising=False)
    monkeypatch.setattr(voice_module.settings, "fallback_enabled", False, raising=False)
    monkeypatch.setattr(voice_module, "update_message_history", _update_message_history)
    monkeypatch.setattr(voice_module, "check_moderation", _check_moderation)
    monkeypatch.setattr(voice_module, "classify_consent", _classify_consent)
    monkeypatch.setattr(voice_module, "normalize_phone_to_mobile", lambda user_id: None)
    monkeypatch.setattr(voice_module, "clean_message_history_for_openai", lambda history: history)
    monkeypatch.setattr(voice_module, "trim_history", lambda history, **kwargs: history)
    monkeypatch.setattr(voice_module, "format_message_pairs", lambda history, limit=None: [])
    monkeypatch.setattr(ob, "set_stage", _set_stage)
    monkeypatch.setattr(ob, "get_stage", _get_stage)
    monkeypatch.setattr(ob, "get_prefetched_milk_summary", _get_prefetched)
    monkeypatch.setattr(ob, "spawn", _spawn)
    monkeypatch.setattr(voice_agent_module.voice_agent, "run_stream", lambda **kw: _run_stream(**kw))
    monkeypatch.setattr(voice_agent_module.voice_agent_signed_in, "run_stream", lambda **kw: _run_stream(**kw))

    async def _collect(query, *, session_id, history=None, call_type="outbound", target_lang="gu"):
        chunks = []
        async for chunk in voice_module.stream_voice_message(
            query=query,
            session_id=session_id,
            source_lang="en",  # skips pretranslation; the consent gate is language-agnostic
            target_lang=target_lang,
            user_id="anonymous",
            history=history or [],
            provider=None,
            process_id="proc-test",
            user_info={},
            owner=None,
            http_request=None,
            call_type=call_type,
        ):
            if isinstance(chunk, str):
                chunks.append(chunk)
        return "".join(chunks)

    state["collect"] = _collect
    return state


def _history_texts(messages):
    out = []
    for msg in messages:
        for part in getattr(msg, "parts", []) or []:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                out.append(content)
    return out


def test_outbound_first_turn_classifies_instead_of_speaking_an_intro(voice_harness):
    """The core of this change: Raya already asked, so turn 1 is the ANSWER.
    We must classify it and never re-ask."""
    from app.services.outbound_consent import ConsentVerdict

    voice_harness["consent"] = ConsentVerdict(intent=INTENT_NEGATIVE, reason="declines")

    output = asyncio.run(voice_harness["collect"]("nahi", session_id="s-first"))

    # The reply reached the classifier on the very first turn.
    assert voice_harness["consent_reply"] == "nahi"
    # ...and was acted on, rather than answered with an intro.
    assert "સરલાબેન બોલું છું" not in output
    assert "વૉટ્સએપ" in output
    assert voice_harness["stage"]["s-first"] == ob.STAGE_RESOLVED


def test_outbound_first_turn_never_arms_the_intro_stage(voice_harness):
    """Nothing may write STAGE_INTRO_SENT any more — that stage existed only to
    mean "we spoke our own intro, awaiting the reply"."""
    from app.services.outbound_consent import ConsentVerdict

    voice_harness["consent"] = ConsentVerdict(intent=INTENT_OTHER, reason="unclear")

    asyncio.run(voice_harness["collect"]("", session_id="s-empty"))

    assert ob.STAGE_INTRO_SENT not in voice_harness["stage"].values()


def test_outbound_first_turn_arms_the_milk_prefetch(voice_harness, monkeypatch):
    """The prefetch used to hide behind our intro turn. With that turn gone it
    must start alongside the consent classifier, or an affirmative reply pays the
    full upstream lookup serially."""
    from app.services import voice as voice_module
    from app.services.outbound_consent import ConsentVerdict

    monkeypatch.setattr(voice_module, "normalize_phone_to_mobile", lambda user_id: "9999999999")
    voice_harness["consent"] = ConsentVerdict(intent=INTENT_OTHER, reason="unclear")

    asyncio.run(voice_harness["collect"]("hmm", session_id="s-prefetch"))

    assert "outbound_milk_prefetch" in voice_harness["prefetch_calls"]


def test_inbound_first_turn_is_untouched(voice_harness):
    """The consent gate must be invisible to the inbound helpline."""
    output = asyncio.run(
        voice_harness["collect"]("my cow has fever", session_id="s-in", call_type="inbound")
    )

    assert "વૉટ્સએપ" not in output
    assert voice_harness.get("consent_reply") is None   # classifier never ran
    assert "s-in" not in voice_harness["stage"]
    assert len(voice_harness["agent_inputs"]) == 1


def test_negative_consent_speaks_the_farewell_then_hangs_up(voice_harness):
    from app.services.outbound_consent import ConsentVerdict

    voice_harness["consent"] = ConsentVerdict(intent=INTENT_NEGATIVE, reason="declines")

    output = asyncio.run(voice_harness["collect"]("na atyare nahi", session_id="s-no"))

    assert "વૉટ્સએપ" in output               # the scripted farewell was spoken
    assert output.rstrip().endswith("Goodbye.")  # exact ASCII telephony token
    assert voice_harness["stage"]["s-no"] == ob.STAGE_RESOLVED
    assert voice_harness["agent_inputs"] == []   # no agent run on a decline


def test_affirmative_consent_hands_the_prefetched_summary_to_the_agent(voice_harness):
    """"ha bolo" is also a _GREETING_TOKENS entry: the consent turn must own it,
    or the most likely affirmative reply gets swallowed by the greeting
    fast-path and the readout is silently lost."""
    from app.services.outbound_consent import ConsentVerdict

    voice_harness["consent"] = ConsentVerdict(intent=INTENT_AFFIRMATIVE, reason="clear yes")
    voice_harness["milk"] = "Milk collection records (2): ... quantity 9 liters"

    asyncio.run(voice_harness["collect"]("ha bolo", session_id="s-yes"))

    assert len(voice_harness["agent_inputs"]) == 1
    hint_text = "\n".join(_history_texts(voice_harness["agent_inputs"][0]))
    assert "quantity 9 liters" in hint_text
    assert "already fetched" in hint_text  # agent told not to re-call the slow tool
    assert voice_harness["stage"]["s-yes"] == ob.STAGE_RESOLVED


def test_other_consent_falls_through_to_a_normal_agent_turn(voice_harness):
    """A farmer who answers the opener with their own question gets it answered."""
    from app.services.outbound_consent import ConsentVerdict

    voice_harness["consent"] = ConsentVerdict(
        intent=INTENT_OTHER, reason="asks own question", failed_open=True,
    )

    output = asyncio.run(
        voice_harness["collect"]("my cow is not eating", session_id="s-other")
    )

    assert "Goodbye." not in output              # an uncertain verdict never hangs up
    assert "વૉટ્સએપ" not in output
    assert len(voice_harness["agent_inputs"]) == 1
    hint_text = "\n".join(_history_texts(voice_harness["agent_inputs"][0]))
    assert "Milk deposit summary" not in hint_text
    assert voice_harness["stage"]["s-other"] == ob.STAGE_RESOLVED


def test_legacy_intro_sent_sessions_still_route_to_the_consent_gate(voice_harness):
    """Sessions mid-call across the deploy already heard our old intro; their
    next turn is still the consent reply and must not be re-classified as turn 1."""
    from app.services.outbound_consent import ConsentVerdict

    voice_harness["stage"]["s-legacy"] = ob.STAGE_INTRO_SENT
    voice_harness["consent"] = ConsentVerdict(intent=INTENT_NEGATIVE, reason="declines")

    output = asyncio.run(
        voice_harness["collect"]("nahi", session_id="s-legacy", history=[object()])
    )

    assert "વૉટ્સએપ" in output
    assert voice_harness["stage"]["s-legacy"] == ob.STAGE_RESOLVED
