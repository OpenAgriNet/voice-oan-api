"""The answer-slot context block in pretranslation (issues #306, #320).

Pretranslation gets the assistant's previous turn on EVERY turn. It used to get
it only when that turn matched the cow-or-buffalo question, which fixed the
species answers and left the next question in the same flow — "which technician
should I book with?" — as broken as before: the farmer has to say a three-part
Gujarati name back and ASR takes it apart ("BAHECHARBHAI" -> "be be char bhai").

These tests pin three things: the context is unconditional, the option-matching
rule refuses to guess between two candidates, and the species corruption table
stays conditional because ગેસ is a real veterinary term (bloat).
"""
import pytest

from app.services.translation import (
    _answer_context,
    _build_openai_pretranslation_messages,
    _build_structured_pretranslation_prompt,
)

SPECIES_Q = "Is this for a cow or a buffalo?"
TECH_Q = (
    "Sangitaben, which technician should I book with? I can book with "
    "Anilbhai Galjibhai Pandar, Narayanbhai Dhulabhai Patel, or "
    "Narendrakumar Narayandas Pandor."
)
CONTEXT = "the assistant's previous turn was"
RULE = "translate it as the option it clearly corresponds to"
SPECIES_HINT = "Common ASR corruptions"


def _system(text, prev=None):
    return _build_openai_pretranslation_messages("Gujarati", "gu", text, prev)[0]["content"]


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
    quoted = _answer_context(long_turn).split('"')[1]
    assert quoted == long_turn
    assert "Narendrakumar Narayandas Pandor" in quoted


def test_structured_fallback_gets_the_same_context():
    """The fallback tier must not silently lose the fix when the primary is down."""
    assert RULE in _build_structured_pretranslation_prompt("Gujarati", "gu", "પંડોર", TECH_Q)
    assert CONTEXT not in _build_structured_pretranslation_prompt("Gujarati", "gu", "પંડોર", None)


def test_user_message_still_carries_only_the_utterance():
    """Context belongs in the system half — the user half stays the raw turn."""
    messages = _build_openai_pretranslation_messages("Gujarati", "gu", " પંડોર ", TECH_Q)
    assert messages[1]["content"] == "પંડોર"


# --- the caller side: what voice.py hands to pretranslation -----------------

class _Part:
    def __init__(self, kind, content):
        self.part_kind = kind
        self.content = content


class _Msg:
    def __init__(self, *parts):
        self.parts = list(parts)


def test_last_assistant_turn_picks_the_most_recent_text():
    from app.services.voice import _last_assistant_turn

    history = [
        _Msg(_Part("user-prompt", "I want AI")),
        _Msg(_Part("text", SPECIES_Q)),
        _Msg(_Part("user-prompt", "cow")),
        _Msg(_Part("text", TECH_Q)),
    ]
    assert _last_assistant_turn(history) == TECH_Q


def test_last_assistant_turn_skips_tool_calls_and_blanks():
    from app.services.voice import _last_assistant_turn

    history = [
        _Msg(_Part("text", TECH_Q)),
        _Msg(_Part("tool-call", "get_ai_technicians")),
        _Msg(_Part("tool-return", "[...]")),
        _Msg(_Part("text", "   ")),
    ]
    assert _last_assistant_turn(history) == TECH_Q


@pytest.mark.parametrize("history", [None, [], [_Msg(_Part("user-prompt", "hello"))]])
def test_last_assistant_turn_empty_history(history):
    from app.services.voice import _last_assistant_turn

    assert _last_assistant_turn(history) is None
