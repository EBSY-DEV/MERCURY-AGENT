"""Who, if anyone, may a draft greet by name?

The Writer calls ``resolve_contact_name`` for a contact. The answer is a first
name it can open with, or nothing, in which case it falls back to a routing
request. A name is only ever returned when there is evidence for it:

* ``named``: the contact already has a name from their own record (a team
  page, an import, a personal address). That always wins; a registry name
  never replaces or overwrites it.
* ``registry``: the contact is a shared inbox (info@, office@, ...) with no
  name of its own, and the company's registry entry lists exactly one
  person Mercury would name (see ``service.pick_person``).
* ``registry_pending``: the same, but a human has not accepted the name yet.
  ``first_name`` stays empty so nothing greets by it; the candidate is in
  ``suggestion`` for review.
* ``none``: no name. A dismissed registry name is ``none`` too.

A registry name is never written onto the prospect. It lives beside it, with
its source, so a reviewer can see where it came from and a better source can
never be overwritten by it.

By default a registry name needs a person's accept before it is used
(``require_review=True``). Pass ``require_review=False`` to use a unique,
matched registry name straight away.
"""

from __future__ import annotations

import re

from mercury.registry.base import normalize_business_name
from mercury.registry.service import pick_person, split_name

# Local parts that name a function or the business, not a person. The one
# place this list lives. Compared with dots, dashes and underscores removed,
# so "customer-service@" and "customerservice@" are the same.
SHARED_LOCAL_PARTS = frozenset({
    "info", "information", "office", "contact", "contactus", "hello", "hi",
    "hey", "sales", "admin", "administrator", "support", "help", "service",
    "services", "customerservice", "customercare", "care", "team", "mail",
    "general", "enquiries", "enquiry", "inquiries", "inquiry", "estimates",
    "estimate", "quotes", "quote", "bookings", "booking", "appointments",
    "reservations", "orders", "frontdesk", "reception", "receptionist",
    "marketing", "billing", "accounts", "accounting", "hr", "jobs",
    "careers", "press", "media", "webmaster", "dispatch", "scheduling",
    "contacto", "ventas", "hola", "oficina", "servicio", "info1",
})


def is_shared_inbox(email: str) -> bool:
    """True when the address belongs to a function or the business, not a
    person: info@, office@, contact@, hello@, sales@, admin@, ..."""
    local = (email or "").strip().lower().split("@", 1)[0]
    local = local.split("+", 1)[0]
    return re.sub(r"[.\-_]", "", local) in SHARED_LOCAL_PARTS


def _get(obj, key, default=""):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key) or default
    return getattr(obj, key, default) or default


def has_own_name(prospect, company=None) -> bool:
    """A real name of the contact's own, not the business or a role standing
    in for one (the public-inbox sweep stores the business name as the first
    name and "Team" as the last)."""
    first = str(_get(prospect, "first_name")).strip()
    if not first:
        return False
    email = str(_get(prospect, "email"))
    shared = is_shared_inbox(email)
    if shared and _get(prospect, "source") == "public_inbox":
        return False
    if re.sub(r"[.\-_ ]", "", first.lower()) in SHARED_LOCAL_PARTS:
        return False
    if shared and str(_get(prospect, "last_name")).strip().lower() == "team":
        return False
    ours = normalize_business_name(first)
    for business in (_get(company, "name"), _get(prospect, "company")):
        if business and ours == normalize_business_name(str(business)):
            return False
    return True


def _result(status, *, first="", full="", title="", source="", url="", **extra) -> dict:
    return {"first_name": first, "full_name": full, "title": title,
            "source": source, "source_url": url, "status": status, **extra}


def resolve_from_evidence(prospect, company, lookup: dict | None, review: dict | None,
                          *, require_review: bool = True) -> dict:
    """The decision, given the stored registry row and review (either may be
    None). Pure: no database, no network."""
    if has_own_name(prospect, company):
        first = str(_get(prospect, "first_name")).strip()
        last = str(_get(prospect, "last_name")).strip()
        if last.lower() == "team":
            last = ""
        return _result("named", first=first, full=f"{first} {last}".strip(),
                       title=str(_get(prospect, "title")),
                       source=str(_get(prospect, "source")) or "prospect",
                       url=str(_get(prospect, "source_url")))

    if not is_shared_inbox(str(_get(prospect, "email"))):
        # A personal address with no usable name: nothing to add.
        return _result("none", reason="no_name")
    if not lookup or lookup.get("status") != "matched":
        return _result("none", reason="no_registry_match")

    person, basis = pick_person(lookup.get("people") or [])
    if person is None:
        return _result("none", reason=basis)
    first, _last = split_name(person["name"])
    if not first:
        return _result("none", reason="initial_only")

    evidence = {"entity_name": lookup.get("entity_name", ""),
                "document_number": lookup.get("document_number", ""),
                "confidence": lookup.get("confidence", 0), "basis": basis}
    suggestion = {"first_name": first, "full_name": person["name"], "title": person["title"]}
    url = lookup.get("source_url", "")

    decided = review if review and review.get("person_name") == person["name"] else None
    if decided and decided["decision"] == "dismissed":
        return _result("none", reason="dismissed", suggestion=suggestion, **evidence)
    if (decided and decided["decision"] == "accepted") or not require_review:
        return _result("registry", first=first, full=person["name"], title=person["title"],
                       source="registry", url=url, reviewed=bool(decided), **evidence)
    return _result("registry_pending", source="registry", url=url,
                   suggestion=suggestion, **evidence)


async def resolve_contact_name(state, prospect, company, *, require_review: bool = True) -> dict:
    """``{first_name, full_name, title, source, source_url, status, ...}`` for
    one contact. Reads the cached registry result and review from ``state``;
    never fetches."""
    if has_own_name(prospect, company):
        return resolve_from_evidence(prospect, company, None, None)
    company_id = str(_get(company, "id") or _get(prospect, "company_id"))
    lookup = await state.get_registry_lookup(company_id) if company_id else None
    pid = str(_get(prospect, "id"))
    review = (await state.get_registry_reviews([pid])).get(pid) if pid else None
    return resolve_from_evidence(prospect, company, lookup, review, require_review=require_review)
