"""The registry result as the Contacts tab and the CLI show it.

``registry_view`` is the one place a contact's registry state is worked out,
so the API and the CLI never disagree. It reads only what is stored: it never
fetches.
"""

from __future__ import annotations

import logging

from mercury.registry.resolver import has_own_name, is_shared_inbox, resolve_from_evidence
from mercury.registry.service import _person_key, pick_person, split_name

logger = logging.getLogger("mercury.registry")

# What a contact's registry status can be.
#   matched        one active entity, name and city agree
#   ambiguous      several plausible entities, or no city to tell them apart
#   no_match       nothing plausible (including: only inactive entities)
#   not_eligible   no supported registry covers the company's location
#   not_looked_up  eligible, never looked up
#   unavailable    the registry refused or failed; will be retried


# The kind of entity, from the legal suffix on its registered name. Empty
# when the name carries none: the filing's own type is not stored.
_ENTITY_KINDS = (
    (("pllc", "p.l.l.c."), "professional limited liability company"),
    (("llc", "l.l.c.", "lc", "l.c."), "limited liability company"),
    (("llp", "l.l.p."), "limited liability partnership"),
    (("lp", "l.p."), "limited partnership"),
    (("pa", "p.a."), "professional association"),
    (("inc", "inc.", "incorporated", "corp", "corp.", "corporation"), "corporation"),
)


def entity_kind(entity_name: str) -> str:
    tail = (entity_name or "").strip().lower().rstrip(",").split(" ")[-1].strip(",")
    for suffixes, kind in _ENTITY_KINDS:
        if tail in suffixes:
            return kind
    return ""


def _jurisdiction_name(provider) -> str:
    if provider is None:
        return ""
    return getattr(provider, "jurisdiction_name", "") or getattr(provider, "jurisdiction", "")


def _view(prospect: dict, company: dict | None, lookup: dict | None,
          review: dict | None, provider, *, require_review: bool = True,
          covered: list[str] | None = None) -> dict:
    email = prospect.get("email") or ""
    view = {
        "status": "not_eligible", "reason": "", "provider": "", "provider_label": "",
        "entity_name": "", "document_number": "", "source_url": "", "confidence": 0,
        "looked_up_at": "", "people_count": 0, "person": None,
        "shared_inbox": is_shared_inbox(email), "has_own_name": has_own_name(prospect, company),
        "name_status": "none", "review": (review or {}).get("decision"),
        # What the drawer shows: where the company is, which jurisdictions
        # Mercury can read, and, for a lookup, the filing's people and the
        # entities it weighed. A person's `suggested` flag is the one name
        # the accept endpoint will take.
        "company_location": (company or {}).get("location", "") or "",
        "jurisdiction_name": _jurisdiction_name(provider),
        "covered": list(covered or []),
        "entity_type": "", "city": "", "people": [], "candidates": [], "on_filing": None,
    }
    if lookup:
        view.update(
            status=lookup["status"], reason=lookup.get("reason", ""),
            provider=lookup.get("provider", ""), entity_name=lookup.get("entity_name", ""),
            document_number=lookup.get("document_number", ""),
            source_url=lookup.get("source_url", ""),
            confidence=lookup.get("confidence") or 0,
            looked_up_at=lookup.get("looked_up_at") or "",
            people_count=len(lookup.get("people") or []),
            city=lookup.get("searched_city") or "",
            entity_type=entity_kind(lookup.get("entity_name", "")))
        if lookup["status"] in ("ambiguous", "no_match"):
            view["candidates"] = [
                {"name": c.get("name", ""), "document_number": c.get("document_number", ""),
                 "status": c.get("status", ""), "url": c.get("url", "")}
                for c in lookup.get("candidates") or []]
        if provider is not None:
            view["provider_label"] = provider.label
        if lookup["status"] == "matched":
            person, basis = pick_person(lookup.get("people") or [])
            if person:
                first, _ = split_name(person["name"])
                view["person"] = {"name": person["name"], "first_name": first,
                                  "title": person["title"], "basis": basis}
            chosen = view["person"]["name"] if view["person"] else None
            view["people"] = [
                {"name": p["name"], "title": p.get("title", ""), "raw_title": p.get("raw_title", ""),
                 "suggested": p["name"] == chosen} for p in lookup.get("people") or []]
            ours = _person_key(f"{prospect.get('first_name', '')} {prospect.get('last_name', '')}")
            if ours and view["has_own_name"]:
                view["on_filing"] = next(
                    ({"name": p["name"], "title": p.get("title", "")}
                     for p in lookup.get("people") or [] if _person_key(p["name"]) == ours), None)
    elif provider is not None:
        view.update(status="not_looked_up", provider=provider.key,
                    provider_label=provider.label)
    resolved = resolve_from_evidence(prospect, company, lookup, review, require_review=require_review)
    view["name_status"] = resolved["status"]
    return view


async def registry_views(state, prospects: list[dict], providers=None) -> dict[str, dict]:
    """``{prospect_id: view}`` for a list of prospect dicts. Never raises: a
    database without the registry tables reads as "not looked up"."""
    from mercury.registry.service import default_providers

    providers = providers if providers is not None else default_providers()
    company_ids = [p.get("company_id") or "" for p in prospects]
    try:
        lookups = await state.get_registry_lookups(company_ids)
        reviews = await state.get_registry_reviews([p.get("id") or "" for p in prospects])
        companies = {}
        for cid in dict.fromkeys(c for c in company_ids if c):
            company = await state.get_company(cid)
            if company:
                companies[cid] = company.model_dump()
    except Exception as e:
        logger.debug("registry view unavailable: %s", e)
        lookups, reviews, companies = {}, {}, {}

    covered = [n for n in (_jurisdiction_name(x) for x in providers) if n]
    out: dict[str, dict] = {}
    for p in prospects:
        company = companies.get(p.get("company_id") or "")
        location = (company or {}).get("location", "")
        provider = next((x for x in providers if x.supports(location)), None)
        out[p.get("id") or ""] = _view(
            p, company, lookups.get(p.get("company_id") or ""),
            reviews.get(p.get("id") or ""), provider, covered=covered)
    return out


async def attach_registry(state, prospects: list[dict]) -> list[dict]:
    """Add a ``registry`` object to each prospect dict (in place)."""
    views = await registry_views(state, prospects)
    for p in prospects:
        p["registry"] = views.get(p.get("id") or "")
    return prospects


class RegistryReviewError(ValueError):
    """Codes: not_found, no_registry_name."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


async def review_registry_name(state, prospect_id: str, decision: str,
                               decided_by: str = "dashboard") -> dict:
    """Accept or dismiss the registry's suggested name for one contact, or
    ``clear`` the decision. Returns the contact's registry view.

    The decision is about a specific person: it applies only while the
    registry still names that person. Nothing is written onto the contact."""
    prospect = await state.get_prospect(prospect_id)
    if prospect is None:
        raise RegistryReviewError("not_found", f"No contact {prospect_id!r}.")
    if decision == "clear":
        await state.clear_registry_review(prospect_id)
    else:
        lookup = await state.get_registry_lookup(prospect.company_id)
        person, _ = pick_person((lookup or {}).get("people") or []) \
            if lookup and lookup["status"] == "matched" else (None, "")
        if person is None:
            raise RegistryReviewError(
                "no_registry_name", "The registry has no single person to accept or dismiss for this company.")
        await state.set_registry_review(
            prospect_id, prospect.company_id, person["name"], decision, decided_by)
    row = prospect.model_dump()
    return (await registry_views(state, [row]))[prospect_id]
