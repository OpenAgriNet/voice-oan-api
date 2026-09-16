

# --- the guard is no longer booking-only -----------------------------------

def test_every_identity_taking_tool_shares_one_guard():
    """The shipped dbc2d23 shape check passes MISSING/UNKNOWN/not_provided —
    they are short and alphanumeric. It only held for create_ai_call because the
    technician-id check did the real work, which is why health call (no
    technician id) kept leaking invented codes at 20%. Every real code carries a
    digit: verified on 28,089 successful bookings, zero exceptions."""
    from agents.tools.identity_guard import invalid_code_field, invalid_technician_id
    for bogus in ("MISSING", "UNKNOWN", "unknown", "not_provided", "not_available",
                  "UNION_CODE_FROM_CONTEXT", "FARMER_CODE_PLACEHOLDER",
                  "Rathod Sanjay Shri Jagats", ""):
        assert invalid_code_field(bogus, "00731", "0554") == "union_code"
        assert invalid_code_field("159", bogus, "0554") == "society_code"
        assert invalid_code_field("159", "00731", bogus) == "farmer_code"
    # real codes are not always numeric — M001 and NA4192 book fine in prod
    for good in ("159", "2021", "M001", "NA4192", "00731", "0554"):
        assert invalid_code_field(good, good, good) is None
    assert invalid_technician_id("QYNWSGoELy1qwA7YfjyJcA==") is False
    assert invalid_technician_id("T55667") is True


def test_empty_farmer_context_is_spelled_out_not_blank():
    """A blank farmer block is what let the agent invent identifiers; the two
    empty states must now be distinguishable AND must forbid the tools."""
    from app.services.voice import _build_compact_farmer_summary

    class _Env:
        def __init__(self, status):
            self.lookupStatus = status
            self.farmers = []

    unresolved = _build_compact_farmer_summary(None)
    not_found = _build_compact_farmer_summary(_Env("not_found"))
    assert unresolved and not_found and unresolved != not_found
    for text in (unresolved, not_found):
        assert "Do not book" in text
        assert "Do not guess or construct" in text


def test_codes_are_checked_against_the_callers_own_accounts():
    """Stronger than any pattern: the model is told to copy these out of the
    farmer context, so with no accounts in context there is nowhere a real code
    could have come from — and with accounts, the triple must be one of them."""
    from agents.tools.identity_guard import codes_absent_from_context

    class _Acct:
        def __init__(self, u, s, f):
            self.union_code, self.society_code, self.farmer_code = u, s, f

    # No accounts resolved is deliberately NOT treated as proof of invention —
    # the tools fall back to model-supplied codes for callers whose context did
    # not load, and shape+digit screens those instead.
    assert codes_absent_from_context([], "159", "00731", "0554") is None
    accounts = [_Acct("159", "00731", "0554"), _Acct("159", "00731", "0192")]
    assert codes_absent_from_context(accounts, "159", "00731", "0554") is None
    assert codes_absent_from_context(accounts, "159", "00731", "0192") is None
    assert codes_absent_from_context(accounts, "159", "00731", "9999")    # another farmer
    assert codes_absent_from_context(accounts, "F12345", "S67890", "U11223")  # invented, digit-bearing
