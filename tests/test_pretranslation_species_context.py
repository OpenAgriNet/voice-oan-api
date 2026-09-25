"""The cow-or-buffalo carve-out in pretranslation (issue #306).

ASR mangles the one-word species answer — ગાય arrives as ગેસ/ગસ/કેસ, ભેંસ as
બસ/બેસ/મેસ — and translating it literally ("for gas", "for mess") strands the
caller in a re-ask loop. The carve-out is deliberately scoped to the turn that
answers the species question, so these tests pin BOTH halves: it engages there,
and it stays out of the way everywhere else.
"""
import pytest

from app.services.translation import (
    _build_openai_pretranslation_messages,
    _build_structured_pretranslation_prompt,
    _species_answer_context,
)

SPECIES_Q = "Is this for a cow or a buffalo?"
CARVE_OUT = "narrows the general 'do not infer animal species' rule"


def _system(text, prev=None):
    return _build_openai_pretranslation_messages("Gujarati", "gu", text, prev)[0]["content"]


def test_carve_out_present_after_the_species_question():
    assert CARVE_OUT in _system("ગેસ માટે", SPECIES_Q)


@pytest.mark.parametrize("prev", [
    None,
    "",
    "Which technician should I book with?",
    "Please wait, I am checking.",
    # near-miss: mentions both animals but is not the species question
    "A cow and a buffalo both need clean water.",
])
def test_carve_out_absent_otherwise(prev):
    assert CARVE_OUT not in _system("ગેસ માટે", prev)


def test_general_conservative_rules_survive_the_carve_out():
    """The carve-out narrows one rule; it must not delete the others."""
    system = _system("ગેસ માટે", SPECIES_Q)
    assert "Do not infer animal species" in system
    assert "Do not repair missing words" in system
    assert "do NOT invent a meaning" in system
    # and it still tells the model to give up when neither species fits
    assert "still say 'unclear animal'" in system


def test_quoted_turn_always_contains_the_question_the_rule_refers_to():
    """The turn is passed whole, so the quote can never lose the species question.

    An earlier cut gated on the full turn but quoted a 300-char prefix, which for
    a long turn handed the model a rule about a question it could no longer see.
    Prod assistant turns top out at 589 chars, so there is nothing to cap.
    """
    long_turn = ("The technician will visit tomorrow morning. " * 20) + SPECIES_Q
    assert len(long_turn) > 600
    quoted = _species_answer_context(long_turn).split('"')[1]
    assert quoted == long_turn
    assert SPECIES_Q in quoted


def test_structured_fallback_gets_the_same_carve_out():
    """The fallback tier must not silently lose the fix when the primary is down."""
    assert CARVE_OUT in _build_structured_pretranslation_prompt(
        "Gujarati", "gu", "ગેસ માટે", SPECIES_Q
    )
    assert CARVE_OUT not in _build_structured_pretranslation_prompt(
        "Gujarati", "gu", "ગેસ માટે", None
    )


def test_user_message_still_carries_only_the_utterance():
    """Context belongs in the system half — the user half stays the raw turn."""
    messages = _build_openai_pretranslation_messages("Gujarati", "gu", " ગેસ માટે ", SPECIES_Q)
    assert messages[1]["content"] == "ગેસ માટે"


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
        _Msg(_Part("text", "Which farmer?")),
        _Msg(_Part("user-prompt", "Ramesh")),
        _Msg(_Part("text", SPECIES_Q)),
    ]
    assert _last_assistant_turn(history) == SPECIES_Q


def test_last_assistant_turn_skips_tool_calls_and_blanks():
    from app.services.voice import _last_assistant_turn

    history = [
        _Msg(_Part("text", SPECIES_Q)),
        _Msg(_Part("tool-call", "get_ai_technicians")),
        _Msg(_Part("tool-return", "[...]")),
        _Msg(_Part("text", "   ")),
    ]
    assert _last_assistant_turn(history) == SPECIES_Q


@pytest.mark.parametrize("history", [None, [], [_Msg(_Part("user-prompt", "hello"))]])
def test_last_assistant_turn_empty_history(history):
    from app.services.voice import _last_assistant_turn

    assert _last_assistant_turn(history) is None
