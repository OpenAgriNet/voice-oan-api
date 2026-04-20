"""
Prompt-only behavior checks for vague-query handling.

These tests do not call real LLM APIs. They validate the prompt contract by
reading canonical User/Assistant examples from prompt files.
"""
from pathlib import Path
import re


REPO_ROOT = Path(__file__).resolve().parent.parent
EN_PROMPT = REPO_ROOT / "assets" / "prompts" / "voice_system_en.md"
GU_PROMPT = REPO_ROOT / "assets" / "prompts" / "voice_system_gu.md"


def _extract_examples(prompt_text: str) -> dict[str, str]:
    pairs = re.findall(r"User:\s*(.+)\nAssistant:\s*(.+)", prompt_text)
    return {user.strip(): assistant.strip() for user, assistant in pairs}


def _contains_gujarati(text: str) -> bool:
    return bool(re.search(r"[\u0A80-\u0AFF]", text))


def run_prompt(user_query: str) -> str:
    """
    Deterministic prompt harness for tests.
    Returns the assistant line from the prompt's canonical examples.
    """
    prompt_path = GU_PROMPT if _contains_gujarati(user_query) else EN_PROMPT
    examples = _extract_examples(prompt_path.read_text(encoding="utf-8"))
    if user_query not in examples:
        raise KeyError(f"No prompt example found for query: {user_query}")
    return examples[user_query]


def test_strict_rule_block_present_in_both_prompts():
    required_snippets = [
        "## VAGUE QUERY HANDLING (STRICT RULE)",
        "Ask EXACTLY ONE clarification question",
        "Maximum 15 words",
        "After asking the question, STOP.",
        "This rule OVERRIDES all other instructions.",
    ]
    for path in (EN_PROMPT, GU_PROMPT):
        text = path.read_text(encoding="utf-8")
        for snippet in required_snippets:
            assert snippet in text


def test_single_question_only():
    response = run_prompt("My cow is not giving milk")
    assert response.count("?") == 1


def test_no_explanation():
    response = run_prompt("My cow is not giving milk")
    assert "because" not in response.lower()
    assert "due to" not in response.lower()


def test_max_15_words():
    response = run_prompt("My cow is not giving milk")
    assert len(response.split()) <= 15


def test_no_multiple_questions():
    response = run_prompt("My cow is not giving milk")
    assert response.count("?") == 1


def test_gujarati_single_question():
    response = run_prompt("મારી ગાય દૂધ નથી આપતી")
    assert response.count("?") == 1


def test_reproduction_case():
    response = run_prompt("My buffalo is not coming in heat")
    assert response.count("?") == 1
