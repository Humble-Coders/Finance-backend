"""GET /capabilities — the region + plan resolution payload (PRD §4.6).

Clients render their UI from this. It is the ONLY thing that decides what a
user sees; there are no per-country branches in client code.

The payload controls what is SHOWN. Every gated endpoint must independently
re-check `resolve()` for what is ALLOWED — a hidden feature is not a secured
feature.

STUB: country packs and plan entitlements are not yet in the database. The
shape is final; the source of the data is not.
"""

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.auth import AuthenticatedUser, current_user

router = APIRouter(tags=["capabilities"])


class Feature(BaseModel):
    enabled: bool
    reason: str | None = None


class Capabilities(BaseModel):
    region: str
    currency: str
    locale: str
    features: dict[str, Feature]
    content: dict[str, object]


def resolve(country_code: str, plan: str) -> Capabilities:
    """The single resolver: plan + region + rollout flags compose here.

    There is exactly one of these. Plan entitlements, region gating and beta
    rollout are NOT separate systems (PRD §4.6).
    """
    # TODO: read country_pack + feature_availability from the database.
    return Capabilities(
        region=country_code,
        currency="CAD",
        locale="en-CA",
        features={
            "bank_linking": Feature(enabled=False, reason="coming_soon"),
            "tax_optimization": Feature(enabled=False, reason="coming_soon"),
            "split_expenses": Feature(enabled=False, reason="coming_soon"),
            "investment_tracking": Feature(enabled=False, reason="region_unsupported"),
        },
        content={
            "tax_accounts": ["RRSP", "TFSA", "FHSA"],
            "disclaimer_version": "ca-v1",
        },
    )


@router.get("/capabilities", response_model=Capabilities)
async def get_capabilities(
    user: AuthenticatedUser = Depends(current_user),
) -> Capabilities:
    # TODO: look up the caller's household country_code and plan.
    return resolve(country_code="CA", plan="free")
