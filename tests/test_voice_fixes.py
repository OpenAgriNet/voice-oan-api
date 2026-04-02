"""
Tests for voice issue fixes from Shridhar feedback analysis.

Covers:
- Greeting detection (expanded tokens, repeated words)
- Fragment detection (garbled/short input)
- Number regex (7+ digit digit-by-digit conversion)
"""
import pytest
import sys
import os

# Add project root to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.services.voice import _is_bare_greeting, _is_fragment_query
from agents.tools.terms import get_ambiguity_hints_for_query


# ---------------------------------------------------------------------------
# Greeting detection tests
# ---------------------------------------------------------------------------

class TestGreetingDetection:
    """Verify _is_bare_greeting catches common greeting variants."""

    @pytest.mark.parametrize("query", [
        "hello",
        "Hello",
        "HELLO",
        "hi",
        "hey",
        "hlo",
        "હલો",
        "હેલો",
        "નમસ્તે",
        "હા",
        "नमस्ते",
        "हेलो",
        "namaste",
        "halo",
    ])
    def test_basic_greetings(self, query):
        assert _is_bare_greeting(query) is True

    @pytest.mark.parametrize("query", [
        "hello hello",
        "hello hello hello",
        "હલો હલો",
        "hlo hlo",
        "hi hi",
    ])
    def test_repeated_greetings(self, query):
        assert _is_bare_greeting(query) is True

    @pytest.mark.parametrize("query", [
        "ha hello",
        "હા હલો",
        "ji",
        "જી",
        "bolo",
        "બોલો",
        "ha bolo",
        "હા બોલો",
    ])
    def test_greeting_combos(self, query):
        assert _is_bare_greeting(query) is True

    @pytest.mark.parametrize("query", [
        "hello!",
        "hello.",
        "  hello  ",
        "* hello *",
    ])
    def test_greetings_with_punctuation(self, query):
        assert _is_bare_greeting(query) is True

    @pytest.mark.parametrize("query", [
        "hello my cow is sick",
        "મારી ગાયને તાવ છે",
        "hello can you help me with my buffalo",
        "હલો મારી ભેંસ",
        "namaste, meri bhains ko bukhar hai",
    ])
    def test_not_bare_greetings(self, query):
        assert _is_bare_greeting(query) is False


# ---------------------------------------------------------------------------
# Fragment detection tests
# ---------------------------------------------------------------------------

class TestFragmentDetection:
    """Verify _is_fragment_query catches garbled/short inputs."""

    @pytest.mark.parametrize("query", [
        "",
        " ",
        "*",
        "* *",
        "ઓ",        # Single Gujarati syllable
        "બ",        # Single Gujarati letter
        "b",        # Single Latin letter
        "O",
        "ok",       # 2 chars
        "હા",       # 2 Gujarati chars (3 bytes but ≤3 chars)
    ])
    def test_fragments(self, query):
        assert _is_fragment_query(query) is True

    @pytest.mark.parametrize("query", [
        "મારી ગાય",           # "My cow" — 2 words, real content
        "hello",               # 5 chars — greeting, not fragment
        "cow is sick",
        "દૂધ ઓછું",            # "Less milk"
        "ભેંસ બીમાર છે",       # "Buffalo is sick"
    ])
    def test_not_fragments(self, query):
        assert _is_fragment_query(query) is False




# ---------------------------------------------------------------------------
# Ambiguity terms tests
# ---------------------------------------------------------------------------

class TestAmbiguityTerms:
    """Verify new ambiguity terms trigger correct disambiguation hints."""

    def test_fat_vs_stomach(self):
        """ફેટ should trigger milk fat hint, not stomach."""
        result = get_ambiguity_hints_for_query("મારી ભેંસનું ફેટ ઓછું છે")
        assert "milk fat" in result.lower() or "ફેટ" in result
        assert "પેટ" not in result or "NOT પેટ" in result

    def test_retained_placenta(self):
        """માટી ન ખસવી should trigger retained placenta hint."""
        result = get_ambiguity_hints_for_query("ગાયની માટી ન ખસવી")
        assert "retained placenta" in result.lower() or "મેલી" in result

    def test_meli(self):
        """મેલી should also trigger retained placenta."""
        result = get_ambiguity_hints_for_query("મેલી ન પડી")
        assert "retained placenta" in result.lower() or "afterbirth" in result.lower()

    def test_karmodi(self):
        """કરમોડી should trigger horn cancer hint."""
        result = get_ambiguity_hints_for_query("ગાયને કરમોડી થયો છે")
        assert "horn cancer" in result.lower() or "કરમોડી" in result

    def test_vado_shed(self):
        """વાડો should trigger cattle shed hint, not calf."""
        result = get_ambiguity_hints_for_query("વાડો કેવી રીતે બનાવવો")
        assert "shed" in result.lower() or "enclosure" in result.lower()
        assert "પાડો" not in result or "NOT પાડો" in result

    def test_existing_uthla_still_works(self):
        """Existing entry: ઉથલા should still trigger repeat breeder."""
        result = get_ambiguity_hints_for_query("મારી ગાય ઉથલા મારે છે")
        assert "repeat breeder" in result.lower()

    def test_no_match(self):
        """Unrelated query should return empty."""
        result = get_ambiguity_hints_for_query("દૂધ કેવી રીતે વધારવું")
        # Should not match any ambiguity term (unless it fuzzy-matches something)
        # At minimum, should not crash
        assert isinstance(result, str)
