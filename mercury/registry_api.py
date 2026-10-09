"""Local dashboard API for the public-registry lookup on a company's contacts.

The contact lists (``/api/prospects``, ``/api/companies/{id}/contacts``)
already carry each contact's ``registry`` object; these endpoints refresh a
company's lookup and record a person's accept/dismiss of the registry's name.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict

from mercury.registry.contacts import RegistryReviewError, review_registry_name
from mercury.registry.service import RegistryService, default_providers

router = APIRouter()
ACTOR = "dashboard"


class ReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: str   # accepted | dismissed | clear


async def _state():
    from mercury.dashboard import _state as make_state
    state = make_state()
    await state.init_db()
    return state


def _result(result) -> dict:
    return result.as_dict()


@router.get("/api/companies/{company_id}/registry")
async def get_company_registry(company_id: str):
    """The stored registry result for a company. Never fetches."""
    state = await _state()
    company = await state.get_company(company_id)
    if company is None:
        raise HTTPException(404, {"code": "not_found", "message": "No such company."})
    service = RegistryService(state, default_providers())
    cached = await service.cached(company)
    if cached:
        return _result(cached)
    provider = service.provider_for(company.location)
    status = "not_looked_up" if provider else "not_eligible"
    return {"status": status, "provider": provider.key if provider else "",
            "provider_label": provider.label if provider else ""}


@router.post("/api/companies/{company_id}/registry/refresh")
async def refresh_company_registry(company_id: str):
    """Look the company up again, now. Slow on purpose: requests to the
    registry are spaced out. 409 when the CONTACT_FOUND signal is not confirmed."""
    state = await _state()
    company = await state.get_company(company_id)
    if company is None:
        raise HTTPException(404, {"code": "not_found", "message": "No such company."})
    service = RegistryService(state, default_providers())
    try:
        result = await service.lookup(company, force=True)
    finally:
        await service.aclose()
    if result.status == "skipped":
        raise HTTPException(409, {"code": "signal_not_confirmed", "message": result.reason +
                                  ". Confirm it on the Signals tab first."})
    return _result(result)


@router.post("/api/contacts/{prospect_id}/registry-name")
async def review_contact_registry_name(prospect_id: str, body: ReviewInput):
    """Accept or dismiss the registry's name for this contact, or clear the
    decision. Nothing on the contact is overwritten."""
    if body.decision not in ("accepted", "dismissed", "clear"):
        raise HTTPException(422, {"code": "invalid",
                                  "message": "decision must be accepted, dismissed or clear."})
    state = await _state()
    try:
        return await review_registry_name(state, prospect_id, body.decision, ACTOR)
    except RegistryReviewError as e:
        raise HTTPException(404 if e.code == "not_found" else 409,
                            {"code": e.code, "message": str(e)}) from e
