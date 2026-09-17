"""Whether the caller's farmer identity is resolved for this turn, and what
follows from that.

Voice used to render "no farmer record exists" and "we have not established
whether one exists" identically — as an empty farmer block — while still
offering every identity-taking tool. The model, handed a required `farmer_code`
slot and nothing to fill it from, invented one: `MISSING`, `UNKNOWN`, `F12345`,
`UNION_CODE_FROM_CONTEXT`, farmer names in `farmerCode`. Measured 2026-09-01..09-14
on voice-production: create_health_call 20.2%, create_ai_call 10.4%,
get_farmer_milk_collection_details 10.2%. Every observed failure ran on a prompt
with no farmer block, and no booking has ever succeeded from one.

This module is the ONE place that decides the state, because two consumers must
never disagree about it:

  * `prepare_requires_farmer_identity` removes the identity-taking tools from
    the schema the model sees, so inventing a code is not something it can do.
  * `unavailable_capability_lines` tells the model which capabilities are off
    this turn and why, so it says something true instead of "that feature does
    not exist" or, worse, claiming a booking it never made.

If those were computed separately they would drift, and a prompt that promises a
tool which is not in the schema is worse than either failure alone.

See issue #282.
"""
from typing import Literal, Optional, Sequence

from pydantic_ai import RunContext
from pydantic_ai.tools import ToolDefinition

from agents.deps import FarmerAccount, FarmerContext, FarmerTechnician
from agents.models.farmer import FarmerDataEnvelope
from helpers.utils import get_logger

logger = get_logger(__name__)

# found      — the caller has at least one farmer record we can act on.
# not_found  — upstream confirmed this mobile has no farmer record. Terminal for
#              the call: the caller must register with their milk society.
# unresolved — we do not know. The cold fetch timed out, a concurrent refresh
#              held the lock, or the read failed. Recoverable: a later turn may
#              well resolve it, so the caller is told to retry, never that the
#              service does not exist.
FarmerIdentityState = Literal["found", "not_found", "unresolved"]

FOUND: FarmerIdentityState = "found"
NOT_FOUND: FarmerIdentityState = "not_found"
UNRESOLVED: FarmerIdentityState = "unresolved"

# Named in the context block so the model can tell the caller what it cannot do
# this turn. Kept in sync with IDENTITY_GATED_TOOLS below by
# test_farmer_identity_gate.py.
_GATED_CAPABILITY_NAMES = (
    "AI call booking",
    "health call booking",
    "milk collection lookup",
)

# The tools whose parameters are farmer identity codes. Any tool added here must
# also carry `prepare=prepare_requires_farmer_identity` in agents/tools/__init__.py.
IDENTITY_GATED_TOOLS = (
    "create_ai_call",
    "create_health_call",
    "get_farmer_milk_collection_details",
)


def identity_state_for_envelope(
    envelope: Optional[FarmerDataEnvelope],
) -> FarmerIdentityState:
    """Derive the turn's identity state from the farmer-cache read.

    A bare None is the signal that the lookup did not complete: both
    `refresh_farmer_data_bounded` (cold-fetch timeout) and `refresh_farmer_data`
    (refresh lock still held) return None rather than an envelope. That is
    "unresolved", never "not_found" — telling a registered farmer their number
    is unregistered because our fetch was slow is its own bug.
    """
    if envelope is None:
        return UNRESOLVED
    if envelope.farmers:
        return FOUND
    if envelope.lookupStatus == "not_found":
        return NOT_FOUND
    # An empty envelope with no explicit verdict: we cannot claim upstream said
    # "no such farmer", so treat it as not-yet-known.
    return UNRESOLVED


def identity_state_for_deps(deps: Optional[FarmerContext]) -> FarmerIdentityState:
    """Read the state off the per-turn deps, defaulting to unresolved.

    Tests and non-voice callers build a FarmerContext without the field, and an
    absent state must fail closed — the whole point is that we do not act on an
    identity we cannot see.
    """
    if deps is None:
        return UNRESOLVED
    state = getattr(deps, "farmer_identity", None)
    if state in (FOUND, NOT_FOUND, UNRESOLVED):
        return state
    return UNRESOLVED


def has_usable_farmer_identity(deps: Optional[FarmerContext]) -> bool:
    """True only when this turn can act on a real, known farmer account.

    Delegates to FarmerContext.has_usable_farmer_identity, which owns the
    predicate. The fallback path exists only for the SimpleNamespace deps stubs
    in the tool tests, and applies the same rule.
    """
    if deps is None:
        return False
    checker = getattr(deps, "has_usable_farmer_identity", None)
    if callable(checker):
        return bool(checker())
    if identity_state_for_deps(deps) != FOUND:
        return False
    return bool(getattr(deps, "farmer_accounts", None))


async def prepare_requires_farmer_identity(
    ctx: RunContext[FarmerContext], tool_def: ToolDefinition
) -> Optional[ToolDefinition]:
    """Withhold an identity-taking tool on a turn with no usable identity.

    Returning None drops the tool from the schema for this run, so the model
    cannot call it. This is deliberately not a prompt instruction: the prompt
    already tells the model to say the details are unavailable
    (assets/prompts/voice_system_translation_pipeline_en.md, and the
    create_ai_call docstring), and that instruction is what the measured 10-20%
    failure rate ignores. An instruction is a request; an absent tool is not.
    """
    if has_usable_farmer_identity(ctx.deps if ctx else None):
        return tool_def
    logger.info(
        "Hiding %s: farmer identity state=%s accounts=%s",
        tool_def.name,
        identity_state_for_deps(ctx.deps if ctx else None),
        len(getattr(ctx.deps, "farmer_accounts", []) or []) if ctx and ctx.deps else 0,
    )
    return None


def unavailable_capability_lines(state: FarmerIdentityState) -> list[str]:
    """Context lines naming what is off this turn and what to tell the caller.

    Phrased as availability status rather than prohibition. "Do not book" implies
    the model still could, which invites it to try or to apologise oddly for a
    tool it cannot see; "unavailable this turn" just hands it a fact to relay.

    The two negative states give the caller DIFFERENT next actions — register vs.
    retry — which is the reason the state is tri-valued rather than a boolean.
    """
    if state == FOUND:
        return []
    capabilities = ", ".join(_GATED_CAPABILITY_NAMES)
    if state == NOT_FOUND:
        return [
            "- Farmer identity: no farmer record is registered for this mobile number.",
            f"- Unavailable for this turn: {capabilities}.",
            "- Tell the caller their number is not registered and ask them to "
            "contact their milk society.",
            "- Do not guess or construct union, society, farmer or technician codes.",
        ]
    return [
        "- Farmer identity: could not be loaded for this turn.",
        f"- Unavailable for this turn: {capabilities}.",
        "- Tell the caller you cannot fetch their details right now and ask them "
        "to try again shortly. Do not say the service does not exist.",
        "- Do not guess or construct union, society, farmer or technician codes.",
    ]


def identity_tool_groups(deps: Optional[FarmerContext]) -> list[str]:
    """Tool groups actually available this turn, for the runtime context line.

    Derived from the same predicate the gates use, so the line can never claim a
    tool group the model has not been given. It previously hardcoded "booking",
    advertising it on precisely the turns where booking was impossible.
    """
    groups = ["retrieval"]
    if has_usable_farmer_identity(deps):
        groups.append("booking")
    # Unchanged condition. This group's tools (get_union_scheme_data,
    # get_farmer_bonus_amount) carry their own prepare gates and resolve their
    # codes server-side from farmer_accounts, so they are not part of the
    # invented-identifier class and must not be withheld here — gating them on
    # identity too would make this line understate what the model was given.
    if getattr(deps, "signed_in", False) and getattr(deps, "mobile", None):
        groups.append("signed-in-farmer-data")
    return groups


def _name_matches(candidate: str, spoken: str) -> bool:
    """Loose match for a name the caller spoke and the model passed through."""
    a = (candidate or "").strip().casefold()
    b = (spoken or "").strip().casefold()
    if not a or not b:
        return False
    return a == b or b in a or a in b


def match_technician(
    technicians: "Sequence[FarmerTechnician]", spoken_name: str
) -> tuple[Optional[FarmerTechnician], Optional[str]]:
    """Resolve a spoken technician name to one option, or say what to ask.

    Returns (technician, None) on a single match, else (None, message).
    """
    if not technicians:
        return None, "No AI technician is available for this caller right now."
    matches = [t for t in technicians if _name_matches(t.full_name or "", spoken_name)]
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        names = ", ".join(t.full_name for t in technicians if t.full_name)
        return None, f"No technician matched that name. Available: {names}."
    names = ", ".join(
        f"{t.full_name} ({t.farmer_name or 'unknown farmer'})" for t in matches
    )
    return None, f"More than one technician matches that name. Ask which: {names}."


def match_account(
    accounts: "Sequence[FarmerAccount]", farmer_name: Optional[str] = None
) -> tuple[Optional[FarmerAccount], Optional[str]]:
    """Resolve which of the caller's accounts to act on.

    One account needs no choice. Several need the farmer's name, because the
    model must not pick silently.
    """
    if not accounts:
        return None, "No farmer account is available for this caller."
    if len(accounts) == 1:
        return accounts[0], None
    if not farmer_name:
        names = ", ".join(a.farmer_name or "unnamed" for a in accounts)
        return None, f"Several farmers are registered on this number. Ask which one: {names}."
    matches = [a for a in accounts if _name_matches(a.farmer_name or "", farmer_name)]
    if len(matches) == 1:
        return matches[0], None
    names = ", ".join(a.farmer_name or "unnamed" for a in accounts)
    return None, f"That farmer name did not match one account. Ask which one: {names}."
