"""Public-registry lookup: matching, abstaining, caching, and the resolver.

No test touches the network: a fake fetch serves synthetic Sunbiz-shaped pages
(tests/registry_fixtures.py) and counts every request it is asked for.
"""

import os
import tempfile
import pytest
import pytest_asyncio

from mercury.models.company import Company
from mercury.models.prospect import Prospect
from mercury.registry import (
    RegistryService, is_shared_inbox, location_city, location_state,
    normalize_business_name, resolve_contact_name,
)
from mercury.registry.base import display_name
from mercury.registry.resolver import resolve_from_evidence
from mercury.registry.service import merge_people, pick_person
from mercury.registry.sunbiz import SunbizProvider, expand_title
from mercury.signals import seed_signal_catalog
from mercury.state import StateManager
from tests import registry_fixtures as fx
from tests.registry_fixtures import FakeSunbiz, provider


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmp:
        sm = StateManager(os.path.join(tmp, "t.db"))
        await sm.init_db()
        await seed_signal_catalog(sm)
        yield sm


async def confirm(state, *codes):
    for code in codes or ("CONTACT_FOUND", "REGISTRY_VERIFIED", "LIKELY_OWNER"):
        await state.set_signal_status(code, "confirmed")


_COUNTER = iter(range(10_000))


async def make_company(state, name="Example Palm Roofing", location="Orlando, FL"):
    company = Company(name=name, domain=f"company-{next(_COUNTER)}.example.com", location=location)
    company.id = await state.add_company(company)
    return company


ROOFING = ("EXAMPLE PALM ROOFING, LLC", "L15000000001", "Active")


# ── Normalization ──

@pytest.mark.parametrize("raw,expected", [
    ("Example Palm Roofing LLC", "example palm roofing"),
    ("EXAMPLE PALM ROOFING, L.L.C.", "example palm roofing"),
    ("The Example Palm Roofing Co.", "example palm roofing"),
    ("Example Palm Roofing, Inc", "example palm roofing"),
    ("Example Palm Roofing Corp.", "example palm roofing"),
    ("Example Palm Roofing - Orlando", "example palm roofing"),
    ("Example Palm Roofing | Central Florida LLC", "example palm roofing"),
    ("Example Palm Roofing (Orlando)", "example palm roofing"),
    ("Smith & Sons Roofing", "smith and sons roofing"),
    ("Café Example, Inc.", "cafe example"),
    ("O'Neil Roofing", "oneil roofing"),
    ("Co", "co"),
])
def test_normalize_business_name(raw, expected):
    assert normalize_business_name(raw) == expected


def test_registry_side_keeps_dashes_in_the_legal_name():
    assert normalize_business_name("EXAMPLE - PALM ROOFING LLC",
                                   strip_location_suffix=False) == "example palm roofing"
    assert normalize_business_name("EXAMPLE - PALM ROOFING LLC") == "example"


@pytest.mark.parametrize("raw,state,city", [
    ("Orlando, FL", "FL", "Orlando"),
    ("Miami, Florida", "FL", "Miami"),
    ("Tampa, FL, USA", "FL", "Tampa"),
    ("Miami FL 33101", "FL", ""),
    ("Denver, CO", "CO", "Denver"),
    ("Washington, DC", "DC", "Washington"),
    ("Santo Domingo", "", "Santo Domingo"),
    ("Santo Domingo, Dominican Republic", "", "Santo Domingo"),
    ("", "", ""),
])
def test_location_parsing(raw, state, city):
    assert location_state(raw) == state
    if raw != "Miami FL 33101":
        assert location_city(raw) == city


# ── Parsing ──

def test_parse_search_and_detail():
    p = SunbizProvider(None)
    rows = p.parse_search(fx.search_page(ROOFING, ("EXAMPLE PALM ROOFING INC", "P01000000002", "Inactive")))
    assert [r.document_number for r in rows] == ["L15000000001", "P01000000002"]
    assert rows[0].is_active and not rows[1].is_active
    assert rows[0].detail_url.startswith("https://search.sunbiz.org/Inquiry/CorporationSearch/SearchResultDetail")
    assert "&amp;" not in rows[0].detail_url

    page = fx.detail_page(ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE Q"), ("AMBR", "EXAMPLE HOLDINGS, LLC")])
    d = p.parse_detail(page, rows[0].detail_url)
    assert d.document_number == "L15000000001" and d.is_active
    assert d.principal_city == "Orlando" and d.mailing_city == "Orlando"
    assert [(o.name, o.title, o.is_person) for o in d.officers] == [
        ("Jane Q Doe", "Manager", True), ("EXAMPLE HOLDINGS, LLC", "Authorized Member", False)]
    # The registered agent is a service company, never an officer.
    assert all("AGENT" not in o.name.upper() for o in d.officers)


def test_search_rows_that_are_not_entities_never_become_candidates():
    p = SunbizProvider(None)
    html = fx.search_page(
        ("EXAMPLE PALM ROOFING LLC", "L15000000001", "Active", "flal"),
        ("EXAMPLE PALM ROOFING CORP", "100001", "Active", "domp"),
        ("EXAMPLE PALM ROOFING FOUNDATION INC", "N15000000002", "Active", "domnp"),
        ("EXAMPLE PALM ROOFING OF GEORGIA INC", "F15000000003", "Active", "forp"),
        ("EXAMPLE PALM ROOFING & LOGO OF A PALM", "900001", "Active", "trade"),      # trademark
        ("EXAMPLE PALM ROOFING LLC", "W15000000004", "Active", "reject"),            # rejected filing
        ("EXAMPLE PALM ROOFING SERVICES", "G15000000005", "Active", "fict"),         # a kind not seen yet
    )
    names = [(r.document_number) for r in p.parse_search(html)]
    assert names == ["L15000000001", "100001", "N15000000002", "F15000000003"]
    # A page of only such rows is an answer (nothing), not a changed layout.
    assert p.parse_search(fx.search_page(("X", "900001", "Active", "trade"))) == []


@pytest.mark.parametrize("status,active", [
    ("Active", True), ("ACTIVE", True), ("INACT", False), ("INACT/UA", False),
    ("InActive", False), ("INACTIVE", False), ("NAME HS", False), ("", False),
])
def test_status_variants(status, active):
    from mercury.registry.base import EntityCandidate
    assert EntityCandidate("N", "1", status, "u").is_active is active


def test_titles_written_as_words_and_suffixed_names():
    p = SunbizProvider(None)
    page = fx.detail_page("EXAMPLE PALM ROOFING INC", "100001", people=[
        ("SVP, Secretary", "ROE, RICK W."),
        ("EXECUTIVE VP & CFO", "POE, PAT"),
        ("SVP", "LOE, LEE"),
        ("VP, Real Estate Assets", "MOE, MARY, IV"),
        ("CEO", "Doe, Jane Q."),
        ("President", "Hoe, Hal L., Jr."),
        ("MGRM", "O'NEIL, SAM"),
    ])
    got = [(o.name, o.title) for o in p.parse_detail(page).officers]
    assert got == [
        ("Rick W. Roe", "SVP, Secretary"),
        ("Pat Poe", "Executive VP & CFO"),
        ("Lee Loe", "SVP"),
        ("Mary Moe IV", "VP, Real Estate Assets"),
        ("Jane Q. Doe", "CEO"),
        ("Hal L. Hoe Jr.", "President"),
        ("Sam O'Neil", "Managing Member"),
    ]


def test_ceo_and_president_are_two_leads_so_the_registry_abstains():
    people = merge_people(_officers([("CEO", "DOE, JANE"), ("President", "ROE, RICK"),
                                     ("VP, Facilities", "POE, PAT")]))
    assert pick_person(people) == (None, "several_people")


def test_parse_search_no_results_vs_unrecognized_page():
    p = SunbizProvider(None)
    assert p.parse_search(fx.NO_RESULTS) == []
    from mercury.registry import RegistryUnavailable
    with pytest.raises(RegistryUnavailable):
        p.parse_search("<html><body><h1>Maintenance</h1></body></html>")


def test_title_codes_and_names():
    assert expand_title("P") == "President"
    assert expand_title("PST") == "President, Secretary, Treasurer"
    assert expand_title("VPD") == "Vice President, Director"
    assert expand_title("MGR") == "Manager"
    assert expand_title("Chief Visionary") == "Chief Visionary"
    assert display_name("DOE, JANE Q") == "Jane Q Doe"
    assert display_name("MCDONALD, ANN") == "Ann McDonald"
    assert display_name("JANE DOE") == "Jane Doe"


def test_pick_person_rules():
    sole = merge_people(_officers([("MGR", "DOE, JANE")]))
    assert pick_person(sole)[0]["name"] == "Jane Doe"
    corp = merge_people(_officers([("P", "DOE, JANE"), ("VP", "ROE, RICHARD"), ("S", "POE, PAT")]))
    assert pick_person(corp)[0]["name"] == "Jane Doe"
    two_leads = merge_people(_officers([("MGR", "DOE, JANE"), ("MGR", "ROE, RICHARD")]))
    assert pick_person(two_leads) == (None, "several_people")
    no_lead = merge_people(_officers([("VP", "DOE, JANE"), ("S", "ROE, RICHARD")]))
    assert pick_person(no_lead) == (None, "several_people")
    assert pick_person([]) == (None, "no_people")
    # One person holding two codes is still one person.
    both = merge_people(_officers([("P", "DOE, JANE"), ("D", "DOE, JANE")]))
    assert len(both) == 1 and both[0]["title"] == "President, Director"


def _officers(pairs):
    from mercury.registry.base import Officer
    from mercury.registry.sunbiz import looks_like_entity
    return [Officer(name=display_name(n), title=expand_title(c), raw_title=c,
                    is_person=not looks_like_entity(n)) for c, n in pairs]


# ── Lookups ──

@pytest.mark.asyncio
async def test_unique_match_has_source_url_and_observations(state):
    await confirm(state)
    company = await make_company(state)
    prospect = Prospect(company_id=company.id, first_name="Jane", last_name="Doe",
                        email="jane@example-palm.example.com", title="Owner", source="company_website")
    prospect.id = await state.add_prospect(prospect)
    fake = FakeSunbiz([ROOFING, ("EXAMPLE PALM ROOFING SERVICES LLC", "L15000000009", "Active")],
                      {"L15000000001": fx.detail_page(ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})
    svc = RegistryService(state, [provider(fake)])

    r = await svc.lookup(company)

    assert r.status == "matched" and r.reason == "name_and_city"
    assert r.document_number == "L15000000001"
    assert r.source_url.startswith("https://search.sunbiz.org/") and "l15000000001" in r.source_url
    assert r.people == [{"name": "Jane Doe", "title": "Manager", "raw_title": "MGR"}]
    assert r.confidence == 0.95
    assert len(fake.requests) == 2          # one search, one detail page

    obs = {o["signal_code"]: o for o in await state.get_observations(company_id=company.id)}
    contact = obs["CONTACT_FOUND"]
    assert contact["value_text"] == "Jane Doe — Manager"
    assert contact["evidence_url"] == r.source_url and contact["collector"] == "registry"
    assert contact["confidence"] == 0.95
    import json
    detail = json.loads(contact["detail_json"])
    assert detail["document_number"] == "L15000000001" and detail["title"] == "Manager"
    assert detail["raw_title"] == "MGR" and detail["provider"] == "fl_sunbiz"
    assert obs["LIKELY_OWNER"]["value_num"] == 1
    # The scraped contact whose full name is on file is confirmed, on that contact.
    assert obs["REGISTRY_VERIFIED"]["prospect_id"] == prospect.id


@pytest.mark.asyncio
async def test_only_confirmed_signals_are_written(state):
    await confirm(state, "CONTACT_FOUND")
    company = await make_company(state)
    fake = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})
    await RegistryService(state, [provider(fake)]).lookup(company)
    codes = {o["signal_code"] for o in await state.get_observations(company_id=company.id)}
    assert codes == {"CONTACT_FOUND"}


@pytest.mark.asyncio
async def test_nothing_is_looked_up_until_the_signal_is_confirmed(state):
    company = await make_company(state)
    fake = FakeSunbiz([ROOFING], {})
    r = await RegistryService(state, [provider(fake)]).lookup(company)
    assert r.status == "skipped" and "CONTACT_FOUND" in r.reason
    assert fake.requests == []
    assert await state.get_registry_lookup(company.id) is None


@pytest.mark.asyncio
async def test_ambiguous_match_abstains_and_is_recorded(state):
    await confirm(state)
    company = await make_company(state)
    fake = FakeSunbiz(
        [ROOFING, ("EXAMPLE PALM ROOFING INC", "P16000000002", "Active")],
        {"L15000000001": fx.detail_page(ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")]),
         "P16000000002": fx.detail_page("EXAMPLE PALM ROOFING INC", "P16000000002",
                                        principal_city="ORLANDO", people=[("P", "ROE, RICHARD")])})
    r = await RegistryService(state, [provider(fake)]).lookup(company)
    assert r.status == "ambiguous" and r.reason == "several_entities_in_city"
    assert r.people == [] and len(r.candidates) == 2
    assert await state.get_observations(company_id=company.id) == []
    assert (await state.get_registry_lookup(company.id))["status"] == "ambiguous"


@pytest.mark.asyncio
async def test_two_same_name_entities_in_different_cities_pick_ours(state):
    await confirm(state)
    company = await make_company(state, location="Tampa, FL")
    fake = FakeSunbiz(
        [ROOFING, ("EXAMPLE PALM ROOFING INC", "P16000000002", "Active")],
        {"L15000000001": fx.detail_page(ROOFING[0], ROOFING[1], principal_city="ORLANDO",
                                        people=[("MGR", "DOE, JANE")]),
         "P16000000002": fx.detail_page("EXAMPLE PALM ROOFING INC", "P16000000002",
                                        principal_city="TAMPA", people=[("P", "ROE, RICHARD")])})
    r = await RegistryService(state, [provider(fake)]).lookup(company)
    assert r.status == "matched" and r.document_number == "P16000000002"
    assert r.people[0]["name"] == "Richard Roe"
    assert r.confidence == 0.95


@pytest.mark.asyncio
async def test_mailing_city_alone_matches_with_lower_confidence(state):
    await confirm(state)
    company = await make_company(state)
    fake = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], principal_city="MIAMI", mailing_city="ORLANDO",
        people=[("MGR", "DOE, JANE")])})
    r = await RegistryService(state, [provider(fake)]).lookup(company)
    assert r.status == "matched" and r.confidence == 0.9


@pytest.mark.asyncio
async def test_no_match_cases(state):
    await confirm(state)
    svc_for = lambda fake: RegistryService(state, [provider(fake)])

    c1 = await make_company(state, "Nothing Like It")
    r = await svc_for(FakeSunbiz([("SOMETHING ELSE LLC", "L1", "Active")], {})).lookup(c1)
    assert (r.status, r.reason) == ("no_match", "no_name_match")

    c2 = await make_company(state, "Example Palm Roofing")
    inactive = FakeSunbiz([("EXAMPLE PALM ROOFING, LLC", "L15000000001", "Inactive")], {})
    r = await svc_for(inactive).lookup(c2)
    assert (r.status, r.reason) == ("no_match", "inactive_only")
    assert len(inactive.requests) == 1       # an inactive entity is never opened

    c3 = await make_company(state, "Example Palm Roofing")
    elsewhere = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], principal_city="MIAMI", people=[("MGR", "DOE, JANE")])})
    r = await svc_for(elsewhere).lookup(c3)
    assert (r.status, r.reason) == ("no_match", "city_mismatch")
    assert r.people == [] and await state.get_observations(company_id=c3.id) == []


@pytest.mark.asyncio
async def test_detail_page_status_wins_over_the_list(state):
    await confirm(state)
    company = await make_company(state)
    fake = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], status="INACTIVE", people=[("MGR", "DOE, JANE")])})
    r = await RegistryService(state, [provider(fake)]).lookup(company)
    assert (r.status, r.reason) == ("no_match", "inactive_only")


@pytest.mark.asyncio
async def test_unknown_city_abstains(state):
    await confirm(state)
    company = await make_company(state, location="Florida")
    fake = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})
    r = await RegistryService(state, [provider(fake)]).lookup(company)
    assert (r.status, r.reason) == ("ambiguous", "city_unknown")


@pytest.mark.asyncio
async def test_results_are_cached_including_no_match_and_ambiguous(state):
    await confirm(state)
    matched = await make_company(state)
    nothing = await make_company(state, "Nothing Like It")
    fake = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})
    svc = RegistryService(state, [provider(fake)])

    first = await svc.lookup(matched)
    miss = await svc.lookup(nothing)
    assert not first.cached and not miss.cached and miss.status == "no_match"
    seen = len(fake.requests)

    again = await svc.lookup(matched)
    miss_again = await svc.lookup(nothing)
    assert again.cached and again.status == "matched" and again.people == first.people
    assert miss_again.cached and miss_again.status == "no_match"
    assert len(fake.requests) == seen            # nothing was fetched a second time

    # A new service (a new process) reads the same stored answer.
    fresh = RegistryService(state, [provider(fake)])
    assert (await fresh.lookup(matched)).cached
    assert len(fake.requests) == seen

    forced = await fresh.lookup(matched, force=True)
    assert not forced.cached and len(fake.requests) == seen + 2


@pytest.mark.asyncio
async def test_non_florida_company_is_not_eligible_and_costs_nothing(state):
    await confirm(state)
    fake = FakeSunbiz([ROOFING], {})
    svc = RegistryService(state, [provider(fake)])
    for location in ("Denver, CO", "Santo Domingo, Dominican Republic", ""):
        company = await make_company(state, location=location)
        r = await svc.lookup(company)
        assert r.status == "not_eligible"
    assert fake.requests == []
    assert await state.get_registry_lookup(company.id) is None


@pytest.mark.asyncio
async def test_eligibility_is_the_providers_jurisdiction(state):
    """A second registry brings its own jurisdiction; nothing else changes."""
    from mercury.registry.base import RegistryProvider

    class Texas(RegistryProvider):
        key, jurisdiction, label = "tx_test", "TX", "Texas test registry"
        async def search(self, name): return []
        async def detail(self, candidate): raise AssertionError

    await confirm(state)
    svc = RegistryService(state, [provider(FakeSunbiz()), Texas()])
    assert svc.provider_for("Austin, TX").key == "tx_test"
    assert svc.provider_for("Orlando, FL").key == "fl_sunbiz"
    assert svc.provider_for("Denver, CO") is None


@pytest.mark.asyncio
async def test_challenge_is_unavailable_not_no_match(state):
    await confirm(state)
    company = await make_company(state)
    fake = FakeSunbiz(search_html=fx.CHALLENGE, status=403)
    svc = RegistryService(state, [provider(fake)])
    r = await svc.lookup(company)
    assert r.status == "unavailable" and "challenge" in r.reason
    # Stops asking for the rest of the session.
    other = await make_company(state, "Another Company")
    assert (await svc.lookup(other)).status == "unavailable"
    assert len(fake.requests) == 1


@pytest.mark.asyncio
async def test_failed_refresh_keeps_the_good_answer(state):
    await confirm(state)
    company = await make_company(state)
    good = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})
    await RegistryService(state, [provider(good)]).lookup(company)

    blocked = RegistryService(state, [provider(FakeSunbiz(search_html=fx.CHALLENGE, status=403))])
    assert (await blocked.lookup(company, force=True)).status == "unavailable"
    kept = await state.get_registry_lookup(company.id)
    assert kept["status"] == "matched" and kept["people"][0]["name"] == "Jane Doe"


@pytest.mark.asyncio
async def test_unavailable_is_retried_after_a_day_not_before(state):
    await confirm(state)
    company = await make_company(state)
    blocked = FakeSunbiz(search_html=fx.CHALLENGE, status=403)
    await RegistryService(state, [provider(blocked)]).lookup(company)

    again = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})
    r = await RegistryService(state, [provider(again)]).lookup(company)
    assert r.status == "unavailable" and r.cached and again.requests == []

    import aiosqlite
    async with aiosqlite.connect(state.db_path) as db:
        await db.execute("UPDATE registry_lookups SET looked_up_at = datetime('now', '-2 days')")
        await db.commit()
    r = await RegistryService(state, [provider(again)]).lookup(company)
    assert r.status == "matched"


@pytest.mark.asyncio
async def test_requests_are_spaced_out():
    sleeps: list[float] = []
    fake = FakeSunbiz([ROOFING], {})
    p = provider(fake, sleeps)
    await p.search("example palm roofing")
    await p.search("example palm roofing")
    await p.search("example palm roofing")
    assert len(sleeps) == 2 and all(s > 0 for s in sleeps)
    assert "MercuryAgent" in __import__("mercury.registry.sunbiz", fromlist=["x"]).USER_AGENT


# ── The resolver ──

def prospect(**kw):
    base = dict(id="p1", company_id="c1", first_name="", last_name="", email="info@example-palm.example.com",
                title="", source="public_inbox", company="Example Palm Roofing")
    return {**base, **kw}


COMPANY = {"id": "c1", "name": "Example Palm Roofing"}
MATCHED = {"status": "matched", "entity_name": "EXAMPLE PALM ROOFING, LLC",
           "document_number": "L15000000001", "confidence": 0.95,
           "source_url": "https://search.sunbiz.org/x",
           "people": [{"name": "Jane Doe", "title": "Manager", "raw_title": "MGR"}]}


def test_is_shared_inbox():
    for e in ("info@x.com", "Office@x.com", "contact@x.com", "hello@x.com", "sales@x.com",
              "admin@x.com", "customer-service@x.com", "contact.us@x.com", "info+web@x.com",
              "ventas@x.com"):
        assert is_shared_inbox(e), e
    for e in ("jane@x.com", "jane.doe@x.com", "jdoe@x.com", "owner@x.com", "", "info.miami@x.com"):
        assert not is_shared_inbox(e), e


def test_named_contact_keeps_their_own_name_over_the_registry():
    p = prospect(first_name="Maria", last_name="Lopez", email="maria@example-palm.example.com",
                 title="Owner", source="company_website", source_url="https://example-palm.example.com/team")
    r = resolve_from_evidence(p, COMPANY, MATCHED, None, require_review=False)
    assert r == {"first_name": "Maria", "full_name": "Maria Lopez", "title": "Owner",
                 "source": "company_website", "source_url": "https://example-palm.example.com/team",
                 "status": "named"}


def test_a_named_contact_on_a_shared_inbox_is_still_named():
    p = prospect(first_name="Maria", last_name="Lopez", source="company_website")
    assert resolve_from_evidence(p, COMPANY, MATCHED, None)["first_name"] == "Maria"


def test_unnamed_shared_inbox_gets_a_pending_registry_suggestion():
    # The public-inbox sweep stores the business as the first name.
    p = prospect(first_name="Example Palm Roofing", last_name="Team", title="Owner")
    r = resolve_from_evidence(p, COMPANY, MATCHED, None)
    assert r["status"] == "registry_pending" and r["first_name"] == "" and r["full_name"] == ""
    assert r["suggestion"] == {"first_name": "Jane", "full_name": "Jane Doe", "title": "Manager"}
    assert r["source"] == "registry" and r["source_url"] == "https://search.sunbiz.org/x"
    assert r["document_number"] == "L15000000001"


def test_accepted_registry_name_is_used_and_dismissed_is_not():
    p = prospect(first_name="Example Palm Roofing", last_name="Team")
    accepted = {"person_name": "Jane Doe", "decision": "accepted"}
    r = resolve_from_evidence(p, COMPANY, MATCHED, accepted)
    assert (r["status"], r["first_name"], r["full_name"], r["title"], r["source"]) == \
        ("registry", "Jane", "Jane Doe", "Manager", "registry")
    dismissed = {"person_name": "Jane Doe", "decision": "dismissed"}
    r = resolve_from_evidence(p, COMPANY, MATCHED, dismissed, require_review=False)
    assert r["status"] == "none" and r["first_name"] == "" and r["reason"] == "dismissed"


def test_a_review_of_a_different_person_does_not_carry_over():
    p = prospect(first_name="Example Palm Roofing", last_name="Team")
    old = {"person_name": "Someone Else", "decision": "accepted"}
    assert resolve_from_evidence(p, COMPANY, MATCHED, old)["status"] == "registry_pending"


def test_review_can_be_waived():
    p = prospect(first_name="", last_name="")
    r = resolve_from_evidence(p, COMPANY, MATCHED, None, require_review=False)
    assert r["status"] == "registry" and r["first_name"] == "Jane"


@pytest.mark.parametrize("lookup", [
    None,
    {"status": "no_match"},
    {"status": "ambiguous"},
    {**MATCHED, "people": []},
    {**MATCHED, "people": [{"name": "Jane Doe", "title": "Manager"}, {"name": "Rick Roe", "title": "Manager"}]},
    {**MATCHED, "people": [{"name": "J Doe", "title": "Manager", "raw_title": "MGR"}]},
])
def test_no_unique_evidence_means_no_name(lookup):
    p = prospect()
    r = resolve_from_evidence(p, COMPANY, lookup, None, require_review=False)
    assert r["status"] == "none" and r["first_name"] == "" and r["full_name"] == ""


def test_registry_never_names_a_personal_address():
    p = prospect(first_name="", last_name="", email="jane@example-palm.example.com")
    assert resolve_from_evidence(p, COMPANY, MATCHED, None, require_review=False)["status"] == "none"


@pytest.mark.asyncio
async def test_resolver_reads_the_stored_lookup_and_review(state):
    await confirm(state)
    company = await make_company(state)
    p = Prospect(company_id=company.id, first_name="Example Palm Roofing", last_name="Team",
                 email="info@example-palm.example.com", title="Owner", source="public_inbox")
    p.id = await state.add_prospect(p)
    stored = await state.get_prospect(p.id)

    assert (await resolve_contact_name(state, stored, company))["status"] == "none"

    fake = FakeSunbiz([ROOFING], {"L15000000001": fx.detail_page(
        ROOFING[0], ROOFING[1], people=[("MGR", "DOE, JANE")])})
    await RegistryService(state, [provider(fake)]).lookup(company)
    pending = await resolve_contact_name(state, stored, company)
    assert pending["status"] == "registry_pending" and pending["first_name"] == ""

    await state.set_registry_review(p.id, company.id, "Jane Doe", "accepted", "test")
    named = await resolve_contact_name(state, stored, company)
    assert named["status"] == "registry" and named["first_name"] == "Jane"
    assert named["source_url"].startswith("https://search.sunbiz.org/")
    # The prospect itself was never rewritten.
    assert (await state.get_prospect(p.id)).first_name == "Example Palm Roofing"
