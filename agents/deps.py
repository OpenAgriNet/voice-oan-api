import asyncio
from typing import Optional, Literal
from pydantic import BaseModel, Field, PrivateAttr


class FarmerAccount(BaseModel):
    """One (union, society, farmer) account tied to the caller's mobile.

    A single mobile can map to several PashuGPT accounts (e.g. a separate
    cow account and buffalo account). Milk-collection lookups fan out over
    all of these so a farmer's data is never missed just because the agent
    happened to pick the wrong account's codes.
    """
    union_code: Optional[str] = None
    society_code: Optional[str] = None
    farmer_code: Optional[str] = None
    farmer_name: Optional[str] = None
    society_name: Optional[str] = None


class FarmerTechnician(BaseModel):
    """An AI technician the caller may pick, with the account to book against.

    The technician's group carries the farmer/society/union codes, so resolving a
    spoken name yields both the technician id and the account — the model never
    handles either.
    """
    user_id: Optional[str] = None
    full_name: Optional[str] = None
    mobile_number: Optional[str] = None
    union_code: Optional[str] = None
    society_code: Optional[str] = None
    farmer_code: Optional[str] = None
    farmer_name: Optional[str] = None


class FarmerContext(BaseModel):
    """Context for the voice agent.

    Args:
        query: The user's question.
        lang_code: The language code of the user's question.
        target_lang: The target language for the response.
        farmer_info: Pre-built markdown string with farmer profile, animals, vet visits.
        ai_technician_info: Pre-built internal AI technician context for booking.
        provider: The provider for the voice service.
        session_id: The session ID for the user.
        process_id: The process ID for tracking and hold messages.
    """
    query: str = Field(description="The user's question.")
    lang_code: str = Field(description="The language code of the user's question.", default='gu')
    target_lang: str = Field(description="The target language for the response (gu=Gujarati, en=English).", default='gu')
    provider: Optional[Literal['RAYA']] = Field(default=None, description="The provider for the voice service - can be RAYA or None.")
    session_id: Optional[str] = Field(default=None, description="The session ID for the user.")
    process_id: Optional[str] = Field(default=None, description="The process ID for tracking and hold messages.")
    farmer_info: str = Field(default="", description="Pre-built markdown farmer context string.")
    farmer_unions: list[str] = Field(default_factory=list, description="Normalized union names derived from the farmer context.")
    ai_technician_info: str = Field(default="", description="Pre-built internal AI technician context string.")
    signed_in: bool = Field(default=False, description="Whether the session is signed in/authenticated for farmer-specific tools.")
    mobile: Optional[str] = Field(default=None, description="Normalized mobile number when available.")
    # Whether this turn's farmer lookup resolved, and how. Drives BOTH the tool
    # gates and the context lines that name what is unavailable — see
    # agents.services.farmer_identity, which owns the vocabulary. Defaults to
    # "unresolved" so a caller that never set it fails closed.
    farmer_identity: Literal["found", "not_found", "unresolved"] = Field(
        default="unresolved",
        description="Farmer identity resolution state for this turn.",
    )
    farmer_accounts: list[FarmerAccount] = Field(
        default_factory=list,
        description="All (union, society, farmer) accounts on the caller's mobile, for multi-account fan-out.",
    )
    ai_technicians: list[FarmerTechnician] = Field(
        default_factory=list,
        description="Bookable AI technicians, already filtered for banned unions.",
    )

    # Handle to the per-turn content-moderation task, which now runs concurrently
    # with the agent (see app.services.voice). Side-effecting tools await it via
    # ensure_in_scope() so a rejected query can never produce a write, even though
    # the agent executes optimistically before the verdict is known.
    _moderation_task: Optional["asyncio.Task"] = PrivateAttr(default=None)

    def set_moderation_task(self, task: Optional["asyncio.Task"]) -> None:
        """Attach the concurrently-running moderation task for tool self-gating."""
        self._moderation_task = task

    async def ensure_in_scope(self) -> bool:
        """Block until the concurrent moderation verdict is known.

        Returns False ONLY when moderation explicitly rejected the query, so
        side-effecting tools (e.g. bookings) refuse instead of performing a write.
        Fail-open (returns True) when no task is attached or moderation errored —
        a flaky moderation check must never drop a real farmer booking.
        """
        task = self._moderation_task
        if task is None:
            return True
        try:
            verdict = await task
        except Exception:
            return True
        return not bool(verdict is not None and getattr(verdict, "rejected", False))

    def _query_string(self):
        """Get the query string for the agrinet agent."""
        return "**User:** " + '"' + self.query + '"'

    def get_farmer_context_string(self) -> Optional[str]:
        """Return the pre-built farmer context markdown string."""
        return self.farmer_info if self.farmer_info else None

    def get_preferred_union_name(self) -> Optional[str]:
        """Get the primary farmer union name when available."""
        return self.farmer_unions[0] if self.farmer_unions else None

    def has_usable_farmer_identity(self) -> bool:
        """True only when this turn can act on a real, known farmer account.

        The single implementation of the predicate: the tool gates, the
        "tool groups in this run" line and the context wording in
        agents.services.farmer_identity all resolve to this, so the prompt can
        never advertise a capability the model was not given.

        Both conditions are required and they are not redundant.
        `farmer_identity` covers the fetch outcome; `farmer_accounts` covers
        records that came back carrying no complete (union, society, farmer)
        triple — app.services.voice._collect_farmer_accounts drops any record
        missing one of the three, so a "found" caller can still have nothing to
        book with.
        """
        return self.farmer_identity == "found" and bool(self.farmer_accounts)

    def get_runtime_context_message(self) -> str:
        """Compact runtime context that stays outside the static system prompt."""
        lines = [
            "Runtime context for this turn:",
            f"- Signed-in session: {'yes' if self.signed_in else 'no'}",
            f"- Normalized mobile available: {'yes' if self.mobile else 'no'}",
        ]
        if self.mobile:
            lines.append(f"- Normalized mobile: {self.mobile}")
        if self.farmer_unions:
            lines.append(f"- Farmer unions: {', '.join(self.farmer_unions)}")
        lines.append("- Core loop language: English")
        # Keyed on resolved identity, not on signed_in: a signed-in caller whose
        # lookup timed out has no usable identity, and this line used to promise
        # farmer-data tools on exactly those turns (issue #282).
        if self.has_usable_farmer_identity():
            lines.append("- Farmer-data tools may be available for this turn.")
        else:
            lines.append("- Farmer-data tools are not available for this turn.")
        if self.farmer_info:
            lines.append("- Farmer context summary:")
            lines.append(self.farmer_info)
        if self.ai_technician_info:
            lines.append("- Internal AI technician context for booking:")
            lines.append("The caller does not know which AI technicians are available unless you tell them by name.")
            lines.append(self.ai_technician_info)
        return "\n".join(lines)

    def get_user_message(self):
        """Get the user message for the agrinet agent."""
        return self._query_string()
