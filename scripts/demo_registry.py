"""Demo data for the Contacts tab's public-registry column and drawer.

A handful of FICTIONAL Florida businesses with shared inboxes, plus the
stored registry answers (``registry_lookups``) and one person's decision
(``registry_name_reviews``) so every state the dashboard can show is on
screen: matched, a name waiting for review, an accepted name, several filings,
no filing, not looked up, and a registry that could not be reached. The
Colorado companies the main seed already creates read as "not covered".

Nothing here is fetched from the network, and every business, person and
filing number is invented. ``seed_demo.py`` calls ``seed_registry`` once.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.signals import seed_signal_catalog

FILING_URL = "https://search.sunbiz.org/Inquiry/CorporationSearch/ByName"


def _person(name, title, raw):
    return {"name": name, "title": title, "raw_title": raw}


# (company, domain, location, contact local part, contact name or None, title,
#  email status, lookup or None, review decision or None)
CASES = [
    ("Bayside Roof & Gutter", "baysideroofgutter.example", "Tampa, FL", "info", None, "", "verified",
     dict(status="matched", reason="name_and_city", entity_name="BAYSIDE ROOF & GUTTER LLC",
          document_number="L24000000101", confidence=0.95,
          people=[_person("Dana Whitfield", "Manager", "MGR"),
                  _person("Robert Whitfield", "Member", "MBR")]), None),
    ("Gulfline Plumbing", "gulflineplumbing.example", "Sarasota, FL", "marco", "Marco Ibarra", "Owner",
     "verified",
     dict(status="matched", reason="name_and_city", entity_name="GULFLINE PLUMBING INC",
          document_number="P24000000202", confidence=0.95,
          people=[_person("Marco Ibarra", "Authorized member", "AMBR")]), None),
    ("Palmetto Air Co", "palmettoair.example", "Orlando, FL", "office", None, "", "risky",
     dict(status="ambiguous", reason="several_entities_in_city", confidence=0,
          candidates=[
              {"name": "PALMETTO AIR CO", "document_number": "L24000000303", "status": "Active", "url": FILING_URL},
              {"name": "PALMETTO AIR CO LLC", "document_number": "L24000000304", "status": "Active", "url": FILING_URL}]),
     None),
    ("Sunrise Pool Service", "sunrisepoolservice.example", "Miami, FL", "ana", "Ana Pereira", "Office Manager",
     "verified", dict(status="no_match", reason="no_name_match"), None),
    ("Cypress Lane Landscaping", "cypresslanelandscaping.example", "Naples, FL", "office", None, "", "verified",
     dict(status="matched", reason="name_and_city", entity_name="CYPRESS LANE LANDSCAPING LLC",
          document_number="L24000000505", confidence=0.9,
          people=[_person("Mei Tanaka", "Managing Member", "MGRM")]), "accepted"),
    ("Pelican Point Roofing", "pelicanpointroofing.example", "Clearwater, FL", "contact", None, "", "verified",
     dict(status="matched", reason="name_and_city", entity_name="PELICAN POINT ROOFING LLC",
          document_number="L24000000606", confidence=0.95,
          people=[_person("Owen Castellan", "Manager", "MGR"),
                  _person("Priya Castellan", "Authorized member", "AMBR")]), None),
    ("Harborview Electric", "harborviewelectric.example", "Fort Myers, FL", "info", None, "", "verified",
     dict(status="unavailable", reason="The registry asked for a human check"), None),
    ("Coastal Fence Works", "coastalfenceworks.example", "Jacksonville, FL", "hello", None, "", "verified",
     None, None),
]


async def seed_registry(sm, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    await seed_signal_catalog(sm)
    await sm.set_signal_status("CONTACT_FOUND", "confirmed")
    added = 0
    for i, (name, domain, location, local, person, title, email_status, lookup, review) in enumerate(CASES):
        created = now - timedelta(days=2 + i)
        cid = await sm.add_company(Company(
            name=name, domain=domain, website=f"https://{domain}", industry="Construction",
            location=location, source="website", created_at=created, updated_at=created))
        if person:
            first, last = person.split(" ", 1)
        else:
            # The inbox sweep stores the business name as the first name.
            first, last = name, "Team"
        pid = await sm.add_prospect(Prospect(
            company_id=cid, first_name=first, last_name=last, title=title,
            email=f"{local}@{domain}", email_status=email_status, email_verified=email_status == "verified",
            status="new", score=60 + i, company=name, industry="Construction",
            source="registry" if person == "Marco Ibarra" else "website",
            created_at=created, updated_at=created))
        added += 1
        if lookup:
            await sm.save_registry_lookup(cid, {
                "provider": "fl_sunbiz", "searched_name": name.lower(),
                "searched_city": location.split(",")[0],
                "source_url": FILING_URL if lookup["status"] == "matched" else "",
                "looked_up_at": (now - timedelta(days=1, hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                **lookup})
        if review:
            await sm.set_registry_review(pid, cid, lookup["people"][0]["name"], review, "dashboard")
    return {"registry_contacts": added}
