"""Look a company up in a public registry, once, and remember the answer.

The flow for one company:

1. Eligible? A company is eligible when the registry's own jurisdiction
   covers its location. Nothing else decides it (not the ICP, not the
   campaign), and an ineligible company costs no request.
2. Cached? One lookup per company. A stored answer, including "no match"
   and "ambiguous", is returned as is unless a refresh is forced.
3. Search by name; keep entities whose normalized name equals ours. Only
   ACTIVE entities count: an inactive entity (dissolved, revoked, withdrawn)
   no longer has people who can answer, and a dead filing with the same name
   is exactly how a wrong person gets matched. If only inactive entities
   exist the answer is ``no_match`` with the reason ``inactive_only``.
4. Read each remaining entity's page and keep those whose principal or
   mailing city is ours. Exactly one is a match. None is ``no_match``; two or
   more is ``ambiguous``; and a company with no city to check against is
   ``ambiguous`` too (a name alone is not enough). Abstaining is an answer,
   and is stored like one.
5. A match stores the entity's people and, for the signals the user has
   confirmed, writes observations.

A registry that refuses or fails is ``unavailable``. That is never read as
"no match"; it is retried after a day, or at once when forced.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from mercury.registry.base import (
    EntityCandidate, EntityDetail, RegistryProvider, RegistryUnavailable,
    location_city, normalize_business_name, normalize_city,
)

logger = logging.getLogger("mercury.registry")

# The signal that must be confirmed before anything is looked up. The others
# are written only when they are confirmed too.
GATE_SIGNAL = "CONTACT_FOUND"
UNAVAILABLE_RETRY = timedelta(hours=24)
MAX_DETAIL_PAGES = 4

# Titles that make a person the one to write to when several are listed.
LEAD_TITLES = frozenset({
    "president", "ceo", "chairman", "manager", "managing member",
    "authorized person", "authorized member", "owner",
})

STATUSES = ("matched", "ambiguous", "no_match", "not_eligible", "not_looked_up",
            "unavailable", "skipped")


@dataclass
class RegistryResult:
    status: str
    reason: str = ""
    provider: str = ""
    entity_name: str = ""
    document_number: str = ""
    source_url: str = ""
    confidence: float = 0.0
    searched_name: str = ""
    searched_city: str = ""
    candidates: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)   # natural persons only
    looked_up_at: str = ""
    cached: bool = False

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


# ── People ──

def merge_people(officers) -> list[dict]:
    """Natural persons only, one entry per person, titles combined."""
    merged: dict[str, dict] = {}
    for o in officers:
        if not o.is_person:
            continue
        key = re.sub(r"[^a-z]", "", _fold(o.name).lower())
        if not key:
            continue
        entry = merged.setdefault(key, {"name": o.name, "titles": [], "raw_titles": []})
        for t in (x.strip() for x in o.title.split(",")):
            if t and t not in entry["titles"]:
                entry["titles"].append(t)
        if o.raw_title and o.raw_title not in entry["raw_titles"]:
            entry["raw_titles"].append(o.raw_title)
    return [{"name": e["name"], "title": ", ".join(e["titles"]),
             "raw_title": ", ".join(e["raw_titles"])} for e in merged.values()]


def pick_person(people: list[dict]) -> tuple[dict | None, str]:
    """The one person Mercury would name, or (None, why not).

    A sole listed person is the person. Among several, only a single holder
    of a lead title (President, Manager, ...) counts; two leads, or none, is
    abstention. A registry listing is evidence of who is on file, not of who
    answers the inbox, so a tie is never broken by guessing.
    """
    if not people:
        return None, "no_people"
    if len(people) == 1:
        return people[0], "sole_person"
    leads = [p for p in people
             if any(t.strip().lower() in LEAD_TITLES for t in p["title"].split(","))]
    if len(leads) == 1:
        return leads[0], "lead_title"
    return None, "several_people"


def split_name(full: str) -> tuple[str, str]:
    """(first, last) from a display name, or ("", "") when the first token is
    only an initial and so cannot be greeted."""
    tokens = full.replace(",", " ").split()
    if len(tokens) < 2:
        return "", ""
    first = tokens[0].strip(".")
    if len(first) < 2:
        return "", ""
    return first, tokens[-1]


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c))


# ── The lookup ──

class RegistryService:
    def __init__(self, state, providers: list[RegistryProvider]):
        self.state = state
        self.providers = providers
        self._blocked: set[str] = set()   # providers that refused us this session

    def provider_for(self, location: str) -> RegistryProvider | None:
        return next((p for p in self.providers if p.supports(location)), None)

    async def aclose(self) -> None:
        for p in self.providers:
            await p.aclose()

    async def cached(self, company) -> RegistryResult | None:
        row = await self.state.get_registry_lookup(company.id)
        return _from_row(row, cached=True) if row else None

    async def lookup(self, company, *, force: bool = False,
                     confirmed: set[str] | None = None) -> RegistryResult:
        """The registry result for one company, from cache when there is one.

        ``confirmed`` is the set of confirmed signal codes (read from state
        when omitted). Without ``CONTACT_FOUND`` confirmed nothing is looked
        up, though an already cached answer is still returned.
        """
        provider = self.provider_for(company.location)
        if provider is None:
            return RegistryResult("not_eligible", reason="no_registry_for_location")

        row = await self.state.get_registry_lookup(company.id)
        if row and not force and not _stale_unavailable(row):
            return _from_row(row, cached=True)

        if confirmed is None:
            confirmed = await self.state.confirmed_signal_codes()
        if GATE_SIGNAL not in confirmed:
            return RegistryResult("skipped", reason=f"{GATE_SIGNAL} signal not confirmed",
                                  provider=provider.key)
        if provider.key in self._blocked:
            return RegistryResult("unavailable", reason="blocked earlier this session",
                                  provider=provider.key)

        try:
            result = await self._search_and_match(provider, company)
        except RegistryUnavailable as e:
            logger.warning("registry %s unavailable for %s: %s", provider.key, company.name, e)
            self._blocked.add(provider.key)
            result = RegistryResult("unavailable", reason=str(e), provider=provider.key,
                                    searched_name=_searched(company))
            # Never replace a real answer with a failed attempt.
            if row is None or row["status"] == "unavailable":
                await self._save(company, result)
            return result

        await self._save(company, result)
        if result.status == "matched":
            await self._observe(company, result, confirmed)
        return result

    async def _save(self, company, result: RegistryResult) -> None:
        record = result.as_dict()
        record["looked_up_at"] = None   # the database stamps it
        await self.state.save_registry_lookup(company.id, record)
        row = await self.state.get_registry_lookup(company.id)
        result.looked_up_at = (row or {}).get("looked_up_at", "") or ""

    async def _search_and_match(self, provider: RegistryProvider, company) -> RegistryResult:
        ours = normalize_business_name(company.name)
        city = location_city(company.location)
        base = dict(provider=provider.key, searched_name=ours, searched_city=city)
        if not ours:
            return RegistryResult("no_match", reason="no_company_name", **base)

        found = await provider.search(ours)
        named = [c for c in found
                 if normalize_business_name(c.name, strip_location_suffix=False) == ours]
        considered = [_cand(c) for c in named]
        if not named:
            return RegistryResult("no_match", reason="no_name_match", **base)

        active = [c for c in named if c.is_active]
        if not active:
            return RegistryResult("no_match", reason="inactive_only",
                                  candidates=considered, **base)
        if len(active) > MAX_DETAIL_PAGES:
            return RegistryResult("ambiguous", reason="too_many_entities",
                                  candidates=considered, **base)

        details: list[EntityDetail] = [await provider.detail(c) for c in active]
        considered = [{**_cand(c), "status": d.status or c.status,
                       "principal_city": d.principal_city, "mailing_city": d.mailing_city}
                      for c, d in zip(active, details)]
        # The detail page is authoritative for status; the list can lag.
        details = [d for d in details if d.is_active]
        if not details:
            return RegistryResult("no_match", reason="inactive_only",
                                  candidates=considered, **base)
        if not normalize_city(city):
            return RegistryResult("ambiguous", reason="city_unknown",
                                  candidates=considered, **base)

        want = normalize_city(city)
        hits = [d for d in details
                if want in (normalize_city(d.principal_city), normalize_city(d.mailing_city))]
        if not hits:
            return RegistryResult("no_match", reason="city_mismatch",
                                  candidates=considered, **base)
        if len(hits) > 1:
            return RegistryResult("ambiguous", reason="several_entities_in_city",
                                  candidates=considered, **base)

        hit = hits[0]
        both = (normalize_city(hit.principal_city) == want
                and normalize_city(hit.mailing_city) == want)
        return RegistryResult(
            "matched", reason="name_and_city", entity_name=hit.name,
            document_number=hit.document_number, source_url=hit.detail_url,
            confidence=0.95 if both else 0.9, candidates=considered,
            people=merge_people(hit.officers), **base)

    # ── Observations ──

    async def _observe(self, company, result: RegistryResult, confirmed: set[str]) -> None:
        """Facts from a matched lookup, for the signals the user confirmed."""
        provenance = {
            "provider": result.provider, "entity_name": result.entity_name,
            "document_number": result.document_number,
            "match": result.reason, "searched_city": result.searched_city,
        }
        rows: list[dict] = []

        def add(code, *, num=None, text="", conf=None, prospect_id="", extra=None):
            if code not in confirmed:
                return
            rows.append({
                "signal_code": code, "company_id": company.id,
                "prospect_id": prospect_id, "collector": "registry",
                "value_num": num, "value_text": text,
                "confidence": result.confidence if conf is None else conf,
                "evidence_url": result.source_url,
                "detail": {**provenance, **(extra or {})},
            })

        for person in result.people:
            add("CONTACT_FOUND",
                text=f"{person['name']} — {person['title']}" if person["title"] else person["name"],
                extra={"title": person["title"], "raw_title": person["raw_title"]})

        if len(result.people) == 1:
            # The only person on file is, in practice, the owner-operator.
            # Written as a likely owner, not a fact.
            p = result.people[0]
            add("LIKELY_OWNER", num=1, text=p["name"], conf=min(result.confidence, 0.8),
                extra={"title": p["title"], "basis": "sole_person"})

        # A scraped contact whose full name is on the registry page is
        # confirmed; that is also what separates a person from a page heading.
        if "REGISTRY_VERIFIED" in confirmed:
            on_file = {_person_key(p["name"]): p for p in result.people}
            for prospect in await self.state.get_contacts_for_company(company.id):
                hit = on_file.get(_person_key(f"{prospect.first_name} {prospect.last_name}"))
                if hit:
                    add("REGISTRY_VERIFIED", num=1, text=hit["name"],
                        prospect_id=prospect.id,
                        extra={"title": hit["title"], "raw_title": hit["raw_title"]})

        if rows:
            await self.state.add_observations(rows)


def _person_key(name: str) -> str:
    """First and last token, letters only: ignores middle names and initials."""
    tokens = [re.sub(r"[^a-z]", "", t) for t in _fold(name).lower().replace(",", " ").split()]
    tokens = [t for t in tokens if t]
    return f"{tokens[0]} {tokens[-1]}" if len(tokens) >= 2 else ""


def _cand(c: EntityCandidate) -> dict:
    return {"name": c.name, "document_number": c.document_number,
            "status": c.status, "url": c.detail_url}


def _searched(company) -> str:
    return normalize_business_name(company.name)


def _stale_unavailable(row: dict) -> bool:
    """A failed attempt is retried after a day, not on every cycle."""
    if row["status"] != "unavailable":
        return False
    stamp = _parse_ts(row.get("looked_up_at"))
    return stamp is None or datetime.now(timezone.utc).replace(tzinfo=None) - stamp > UNAVAILABLE_RETRY


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("T", " ")[:19])
    except (ValueError, TypeError):
        return None


def _from_row(row: dict, *, cached: bool) -> RegistryResult:
    return RegistryResult(
        status=row["status"], reason=row.get("reason", ""), provider=row.get("provider", ""),
        entity_name=row.get("entity_name", ""), document_number=row.get("document_number", ""),
        source_url=row.get("source_url", ""), confidence=float(row.get("confidence") or 0),
        searched_name=row.get("searched_name", ""), searched_city=row.get("searched_city", ""),
        candidates=row.get("candidates") or [], people=row.get("people") or [],
        looked_up_at=row.get("looked_up_at") or "", cached=cached)


def default_providers() -> list[RegistryProvider]:
    from mercury.registry.sunbiz import SunbizProvider
    return [SunbizProvider()]
