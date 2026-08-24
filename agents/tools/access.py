"""Server-side ownership gates for private and mutating farmer tools."""

from __future__ import annotations

import hmac

from agents.deps import FarmerAccount, FarmerContext


class FarmerAccessDenied(ValueError):
    pass


def require_authenticated_farmer(deps: FarmerContext) -> None:
    if not (
        deps
        and getattr(deps, "signed_in", False)
        and getattr(deps, "identity_verified", False)
        and getattr(deps, "mobile", None)
        and getattr(deps, "subject_id", None)
    ):
        raise FarmerAccessDenied(
            "This information or service is available only to the authenticated farmer."
        )


def require_farmer_accounts(deps: FarmerContext) -> list[FarmerAccount]:
    require_authenticated_farmer(deps)
    accounts = list(getattr(deps, "farmer_accounts", None) or [])
    if not accounts:
        raise FarmerAccessDenied(
            "No farmer account is available for the authenticated mobile number."
        )
    return accounts


def resolve_owned_account(
    deps: FarmerContext,
    union_code: str,
    society_code: str,
    farmer_code: str,
) -> FarmerAccount:
    """Return the exact authenticated account; model arguments cannot override it."""
    supplied = tuple(str(value or "").strip() for value in (union_code, society_code, farmer_code))
    for account in require_farmer_accounts(deps):
        candidate = tuple(
            str(value or "").strip()
            for value in (account.union_code, account.society_code, account.farmer_code)
        )
        if all(hmac.compare_digest(left, right) for left, right in zip(supplied, candidate)):
            return account
    raise FarmerAccessDenied(
        "The selected farmer account does not belong to the authenticated caller."
    )


def require_owned_technician(deps: FarmerContext, technician_id: str) -> str:
    require_authenticated_farmer(deps)
    supplied = str(technician_id or "").strip()
    if not supplied or not any(
        hmac.compare_digest(supplied, str(allowed))
        for allowed in (getattr(deps, "ai_technician_ids", None) or [])
    ):
        raise FarmerAccessDenied(
            "The selected technician is not available in the authenticated farmer context."
        )
    return supplied
