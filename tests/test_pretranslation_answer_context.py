"""The conversation context block in pretranslation (issues #306, #320).

Pretranslation gets the recent understood conversation on EVERY turn, and the
answer-slot rule points at the last assistant question in it. Garbled exchanges
and "please repeat" turns are dropped first, so a repeat request in between no
longer hides the question the farmer is answering (session 601f1db4).

History: pretranslation first got the assistant's previous turn on every turn. It used to get
it only when that turn matched the cow-or-buffalo question, which fixed the
species answers and left the next question in the same flow — "which technician
should I book with?" — as broken as before: the farmer has to say a three-part
Gujarati name back and ASR takes it apart ("BAHECHARBHAI" -> "be be char bhai").

These tests pin three things: the context is unconditional, the option-matching
rule refuses to guess between two candidates, and the species corruption table
stays conditional because ગેસ is a real veterinary term (bloat).
"""
import pytest

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from app.services.translation import (
    _build_openai_pretranslation_messages,
    _build_structured_pretranslation_prompt,
    _conversation_context,
)

SPECIES_Q = "Is this for a cow or a buffalo?"
TECH_Q = (
    "Sangitaben, which technician should I book with? I can book with "
    "Anilbhai Galjibhai Pandar, Narayanbhai Dhulabhai Patel, or "
    "Narendrakumar Narayandas Pandor."
)
CONTEXT = "The assistant's last question was"
RULE = "translate it as the option it clearly corresponds to"
SPECIES_HINT = "Common ASR corruptions"


def _conv(prev):
    """A one-question conversation, or none."""
    return [("Assistant", prev)] if prev is not None else None


def _system(text, prev=None, conversation=None):
    if conversation is None:
        conversation = _conv(prev)
    return _build_openai_pretranslation_messages("Gujarati", "gu", text, conversation)[0]["content"]


# --- the gate is gone: any previous turn reaches the translator ------------

@pytest.mark.parametrize("prev", [
    SPECIES_Q,
    TECH_Q,
    "Booking artificial insemination for your cow with Mayurbhai Naranbhai Patel. Shall I confirm?",
    "Vikrambhai, what main symptom are you seeing in your animal?",
    "Please wait, I am checking.",
])
def test_previous_turn_is_quoted_whatever_it_asked(prev):
    system = _system("બે બે ચાર ભાઈ", prev)
    assert CONTEXT in system
    assert prev in system
    assert RULE in system


@pytest.mark.parametrize("prev", [None, "", "   "])
def test_no_previous_turn_adds_nothing(prev):
    assert CONTEXT not in _system("બે બે ચાર ભાઈ", prev)


def test_technician_names_reach_the_translator_verbatim():
    """The whole point: the candidate list is in the quoted turn, not a table."""
    system = _system("પંડોર નરેનભાઈ", TECH_Q)
    for name in ("Anilbhai Galjibhai Pandar",
                 "Narayanbhai Dhulabhai Patel",
                 "Narendrakumar Narayandas Pandor"):
        assert name in system
    assert "spelled EXACTLY as that option appears above" in system


def test_rule_refuses_to_guess_between_two_options():
    """Booking the wrong technician is worse than re-asking.

    Session 86c1f2ed: the farmer's garble "Pandor Narrenbhai Narayanbhai" draws
    from BOTH Narayanbhai Dhulabhai Patel and Narendrakumar Narayandas Pandor.
    """
    system = _system("પંડોર નરેનભાઈ નારાયણભાઈ", TECH_Q)
    assert "fits TWO of the options, or none, do NOT choose" in system
    assert "acting on the wrong option is worse than re-asking" in system


def test_rule_stands_down_when_the_question_offered_no_options():
    assert "this rule adds nothing" in _system("હા", "Please wait, I am checking.")


# --- the species table stays conditional -----------------------------------

def test_species_hint_present_after_the_species_question():
    assert SPECIES_HINT in _system("ગેસ માટે", SPECIES_Q)


@pytest.mark.parametrize("prev", [
    TECH_Q,
    "Vikrambhai, what main symptom are you seeing in your animal?",
    # near-miss: mentions both animals but is not the species question
    "A cow and a buffalo both need clean water.",
    None,
])
def test_species_hint_absent_otherwise(prev):
    """ગેસ is bloat. Ungated, this table turns "my cow has gas" into "cow"."""
    assert SPECIES_HINT not in _system("ગેસ માટે", prev)


def test_general_conservative_rules_survive():
    """The context block narrows one rule on one turn; it must not delete the others."""
    for prev in (SPECIES_Q, TECH_Q, "Please wait, I am checking."):
        system = _system("ગેસ માટે", prev)
        assert "Do not infer animal species" in system
        assert "Do not repair missing words" in system
        assert "do NOT invent a meaning" in system
    assert "still say 'unclear animal'" in _system("ગેસ માટે", SPECIES_Q)


def test_quoted_turn_is_never_truncated():
    """An earlier cut gated on the full turn but quoted a 300-char prefix, which
    for a long turn handed the model a rule about a question it could not see.
    Prod assistant turns top out at 589 chars, so there is nothing to cap."""
    long_turn = ("The technician will visit tomorrow morning. " * 20) + TECH_Q
    assert len(long_turn) > 600
    assert f'last question was: "{long_turn}"' in _conversation_context(_conv(long_turn))


def test_structured_fallback_gets_the_same_context():
    """The fallback tier must not silently lose the fix when the primary is down."""
    assert RULE in _build_structured_pretranslation_prompt("Gujarati", "gu", "પંડોર", _conv(TECH_Q))
    assert CONTEXT not in _build_structured_pretranslation_prompt("Gujarati", "gu", "પંડોર", None)


def test_user_message_still_carries_only_the_utterance():
    """Context belongs in the system half — the user half stays the raw turn."""
    messages = _build_openai_pretranslation_messages("Gujarati", "gu", " પંડોર ", _conv(TECH_Q))
    assert messages[1]["content"] == "પંડોર"


# --- more than one turn ------------------------------------------------------


def test_rule_targets_the_last_assistant_turn_not_an_earlier_one():
    """The species table must not stay armed once the agent has moved on."""
    conversation = [
        ("Assistant", SPECIES_Q),
        ("Farmer", "buffalo"),
        ("Assistant", TECH_Q),
    ]
    system = _system("ગેસ માટે", conversation=conversation)
    assert f'last question was: "{TECH_Q}"' in system
    assert SPECIES_HINT not in system


# --- the caller side: what voice.py hands to pretranslation -----------------

def _exchange(user, *assistant_parts):
    return [
        ModelRequest(parts=[UserPromptPart(content=user)]),
        ModelResponse(parts=list(assistant_parts)),
    ]


def _q(text):
    """How the agent path stores the farmer's translated turn."""
    return '**User:** "' + text + '"'


def _ctx(history):
    from app.services.voice import _pretranslation_context

    return _pretranslation_context(history)


def test_session_601f1db4_repeat_requests_no_longer_hide_the_species_question():
    """Prod, 26 Sep: the farmer's પસ (buffalo) became "pus" because the one
    quoted turn was a no-audio reply, not the species question."""
    history = [
        *_exchange("hello", TextPart(content="Hello, I am Sarlaben.")),
        *_exchange(_q("I want to do AI booking"), TextPart(content=SPECIES_Q)),
        *_exchange(_q("[unclear token] [unclear token]"),
                   TextPart(content="Please repeat that once. I did not understand you clearly.")),
        *_exchange("[stt:no-audio]",
                   TextPart(content="I could not understand your question. Please ask your question again.")),
    ]
    context = _ctx(history)
    assert context == [
        ("Assistant", "Hello, I am Sarlaben."),
        ("Farmer", "I want to do AI booking"),
        ("Assistant", SPECIES_Q),
    ]
    assert SPECIES_HINT in _system("પસ માટે", conversation=context)


def test_runtime_context_never_reaches_the_translator():
    history = [
        ModelRequest(parts=[UserPromptPart(content="Runtime context for this turn:\n- Union code: 2021")]),
        *_exchange(_q("hi"), TextPart(content="How can I help?")),
    ]
    assert all("Union code" not in text for _, text in _ctx(history))


def test_unclear_farmer_turn_drops_only_the_farmer_side():
    """The agent's follow-up to a half-garbled turn is the open question.

    Replay of 26-Sep prod turns: dropping the whole exchange lost "how many months
    ago did the animal last come in heat?" — the question the next turn answered.
    """
    heat_q = "Dhanabhai, how many months ago did the animal last come in heat?"
    history = [
        *_exchange(_q("I want AI"), TextPart(content=SPECIES_Q)),
        *_exchange(_q("that heifer [unclear token]"), TextPart(content=heat_q)),
    ]
    assert _ctx(history)[-1] == ("Assistant", heat_q)
