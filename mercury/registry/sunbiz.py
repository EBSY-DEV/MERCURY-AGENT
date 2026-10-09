"""Florida Division of Corporations (Sunbiz), search.sunbiz.org.

Public records: search by entity name, then read the entity's detail page for
its status, principal and mailing city, and the officers, managers or
authorized persons it lists. Nothing here logs in or reads anything that is
not on those two public pages.

Sunbiz sits behind a bot challenge. A plain HTTP client is often answered with
a "Just a moment" page instead of the record. That is reported as
``RegistryUnavailable`` (never as "no match"), the lookup is not treated as
done, and nothing tries to get around the challenge. The fetch function is
injectable, so a different transport can be supplied without touching the
parsing.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from typing import Awaitable, Callable
from urllib.parse import quote, urljoin

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from mercury.registry.base import (
    EntityCandidate, EntityDetail, Officer, RegistryProvider,
    RegistryUnavailable, LEGAL_SUFFIXES, display_name, tidy_title,
)

logger = logging.getLogger("mercury.registry.sunbiz")

BASE = "https://search.sunbiz.org"
USER_AGENT = ("MercuryAgent/0.2 (public registry lookup; "
              "+https://github.com/EBSY-DEV/MERCURY-AGENT)")
TIMEOUT = httpx.Timeout(connect=8.0, read=20.0, write=8.0, pool=8.0)

# The kind of record a search row points at, from the detail link's
# `aggregateId` prefix (e.g. `aggregateId=flal-L15...-<uuid>`). Only these are
# business entities. Everything else on the list is a different kind of
# record: `trade-` trademarks, `reject-` rejected filings (listed as "Active"),
# and any prefix not seen yet (fictitious names, partnerships, ...), which is
# skipped rather than guessed at.
ENTITY_KINDS = {
    "flal": "Florida LLC or corporation",
    "domp": "Florida profit corporation",
    "domnp": "Florida non-profit corporation",
    "forp": "Foreign profit corporation",
}

Fetch = Callable[[str], Awaitable[tuple[int, str]]]

_CHALLENGE = re.compile(
    r"just a moment|cf-chl|challenge-platform|enable javascript and cookies|"
    r"verify you are human|captcha", re.I)
_NO_RESULTS = re.compile(r"no (matching )?(records|results|entities)|did not match", re.I)

# Sunbiz's title codes. Combined codes (PST, VPD, ...) are spelled out by
# reading the letters left to right.
TITLE_CODES = {
    "P": "President", "VP": "Vice President", "S": "Secretary",
    "T": "Treasurer", "D": "Director", "C": "Chairman", "CEO": "CEO",
    "CFO": "CFO", "COO": "COO", "MGR": "Manager", "MGRM": "Managing Member",
    "AMBR": "Authorized Member", "AP": "Authorized Person",
    "MBR": "Member", "TR": "Trustee", "PR": "President", "OFF": "Officer",
}
_LETTER_TITLES = {"P": "President", "S": "Secretary", "T": "Treasurer",
                  "D": "Director", "C": "Chairman"}
_ENTITY_WORDS = frozenset({
    "holdings", "trust", "group", "enterprises", "properties", "investments",
    "partners", "ventures", "capital", "management", "services", "associates",
    "fund", "estate", "revocable", "family",
})


def expand_title(code: str) -> str:
    """"P" -> "President", "PST" -> "President, Secretary, Treasurer"."""
    raw = re.sub(r"\s+", " ", (code or "").strip())
    key = raw.upper().replace(".", "")
    if key in TITLE_CODES:
        return TITLE_CODES[key]
    # Combined codes read letter by letter: "PST", or "VPD" = VP + D.
    head = ["Vice President"] if key.startswith("VP") else []
    rest = key[2:] if head else key
    if key.isalpha() and len(key) <= 4 and rest and all(c in _LETTER_TITLES for c in rest):
        return ", ".join(head + [_LETTER_TITLES[c] for c in rest])
    return tidy_title(raw)


def looks_like_entity(name: str) -> bool:
    """An LLC or trust listed as a manager is not a person to greet."""
    tokens = re.sub(r"[^a-z0-9 ]", " ", name.lower().replace(".", "")).split()
    if not tokens:
        return True
    return tokens[-1] in LEGAL_SUFFIXES or any(t in _ENTITY_WORDS for t in tokens)


class SunbizProvider(RegistryProvider):
    key = "fl_sunbiz"
    jurisdiction = "FL"
    label = "Florida Division of Corporations"

    def __init__(self, fetch: Fetch | None = None, *, min_interval: float = 3.0,
                 sleep=asyncio.sleep, max_results: int = 25):
        self._fetch_fn = fetch
        self.min_interval = min_interval
        self._sleep = sleep
        self.max_results = max_results
        self._client: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._last = 0.0

    # ── transport ──

    async def _default_fetch(self, url: str) -> tuple[int, str]:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=TIMEOUT, follow_redirects=True,
                headers={"User-Agent": USER_AGENT,
                         "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                         "Accept-Language": "en-US,en;q=0.9"})
        try:
            resp = await self._client.get(url)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            raise RegistryUnavailable(f"could not reach Sunbiz: {e}") from e
        return resp.status_code, resp.text

    async def _get(self, url: str) -> str:
        """One polite request: spaced out, and a challenge is an error."""
        async with self._lock:
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                await self._sleep(wait + random.uniform(0, self.min_interval / 3))
            try:
                status, body = await (self._fetch_fn or self._default_fetch)(url)
            finally:
                self._last = time.monotonic()
        if _CHALLENGE.search(body[:6000]) and not _looks_like_record(body):
            raise RegistryUnavailable("Sunbiz answered with a bot challenge")
        if status != 200:
            raise RegistryUnavailable(f"Sunbiz answered HTTP {status}")
        return body

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ── search ──

    def search_url(self, name: str) -> str:
        """Shaped like the site's own "Next List" link. The site's search form
        POSTs to /ByName; whether a bare GET of this URL starts a fresh search
        has only been checked through a browser, not through this client."""
        term = re.sub(r"\s+", " ", name).strip().lower()
        order = re.sub(r"[^A-Z0-9]", "", term.upper())
        return (f"{BASE}/Inquiry/CorporationSearch/SearchResults?InquiryType=EntityName"
                f"&inquiryDirectionType=ForwardList&searchNameOrder={quote(order)}"
                f"&SearchTerm={quote(term)}&listNameOrder={quote(order)}")

    async def search(self, name: str) -> list[EntityCandidate]:
        html = await self._get(self.search_url(name))
        return self.parse_search(html)

    def parse_search(self, html: str) -> list[EntityCandidate]:
        soup = BeautifulSoup(html, "html.parser")
        rows: list[EntityCandidate] = []
        saw_rows = False
        for tr in soup.find_all("tr"):
            link = tr.find("a", href=re.compile(r"SearchResultDetail", re.I))
            cells = tr.find_all("td")
            if not link or len(cells) < 3:
                continue
            saw_rows = True
            href = link["href"].replace("&amp;", "&")
            kind = re.search(r"aggregateId=([a-z]+)-", href, re.I)
            if not kind or kind.group(1).lower() not in ENTITY_KINDS:
                logger.debug("skipping non-entity search row: %s", _clean(link.get_text(" "))[:40])
                continue
            rows.append(EntityCandidate(
                name=_clean(link.get_text(" ")),
                document_number=_clean(cells[1].get_text(" ")).upper(),
                status=_clean(cells[-1].get_text(" ")),
                detail_url=urljoin(BASE, href),
            ))
            if len(rows) >= self.max_results:
                break
        if not rows and not saw_rows and not _NO_RESULTS.search(soup.get_text(" ")):
            # Neither a result table nor a plain "nothing found": the page
            # changed shape. Say so instead of reporting a false "no match".
            raise RegistryUnavailable("unrecognized Sunbiz search page")
        return rows

    # ── detail ──

    async def detail(self, candidate: EntityCandidate) -> EntityDetail:
        html = await self._get(candidate.detail_url)
        parsed = self.parse_detail(html, candidate.detail_url)
        if not parsed.document_number:
            parsed.document_number = candidate.document_number
        if not parsed.name:
            parsed.name = candidate.name
        return parsed

    def parse_detail(self, html: str, url: str = "") -> EntityDetail:
        soup = BeautifulSoup(html, "html.parser")
        sections = soup.select("div.detailSection")
        if not sections:
            raise RegistryUnavailable("unrecognized Sunbiz detail page")

        name = ""
        title_box = soup.select_one("div.corporationName")
        if title_box:
            paras = [_clean(p.get_text(" ")) for p in title_box.find_all("p")]
            name = paras[-1] if paras else ""

        fields: dict[str, str] = {}
        filing = soup.select_one("div.filingInformation")
        for label in (filing.find_all("label") if filing else []):
            value = label.find_next_sibling("span")
            if value:
                fields[_clean(label.get_text(" ")).lower()] = _clean(value.get_text(" "))

        principal_city = mailing_city = ""
        officers: list[Officer] = []
        for section in sections:
            head = section.find("span")
            heading = _clean(head.get_text(" ")).lower() if head else ""
            if heading.startswith("principal address"):
                principal_city = _city_of(section)
            elif heading.startswith("mailing address"):
                mailing_city = _city_of(section)
            elif re.search(r"officer|director|authorized person|manager|member", heading) \
                    and "registered agent" not in heading:
                officers.extend(_parse_officers(section))

        return EntityDetail(
            name=name, document_number=fields.get("document number", "").upper(),
            status=fields.get("status", ""), detail_url=url,
            principal_city=principal_city, mailing_city=mailing_city,
            officers=_dedupe(officers),
        )


def _looks_like_record(html: str) -> bool:
    return "detailSection" in html or "SearchResultDetail" in html


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").replace("\xa0", " ")).strip()


def _city_of(section: Tag) -> str:
    """The city from the last "CITY, ST 33101" line of an address block."""
    lines = [_clean(s) for s in section.get_text("\n").split("\n")]
    for line in reversed([l for l in lines if l]):
        m = re.match(r"^(.+?),\s*([A-Z]{2})\s*(\d{5}(?:-\d{4})?)?$", line)
        if m:
            return m.group(1).strip().title()
    return ""


def _parse_officers(section: Tag) -> list[Officer]:
    titles = [s for s in section.find_all("span")
              if re.match(r"^\s*Title\b", s.get_text(" "))]
    out: list[Officer] = []
    for span in titles:
        raw_title = _clean(re.sub(r"^\s*Title\s*", "", span.get_text(" ")))
        lines: list[str] = []
        for sib in span.next_siblings:
            if isinstance(sib, Tag) and sib.name == "span" and \
                    re.match(r"^\s*Title\b", sib.get_text(" ")):
                break
            text = sib.get_text("\n") if isinstance(sib, Tag) else str(sib)
            lines.extend(l for l in (_clean(x) for x in text.split("\n")) if l)
        if not lines:
            continue
        is_person = not looks_like_entity(lines[0])
        # A company is kept as the registry wrote it; only people are reordered.
        name = display_name(lines[0]) if is_person else lines[0]
        if not name:
            continue
        out.append(Officer(name=name, title=expand_title(raw_title),
                           raw_title=raw_title, is_person=is_person))
    return out


def _dedupe(officers: list[Officer]) -> list[Officer]:
    seen: set[tuple[str, str]] = set()
    out = []
    for o in officers:
        key = (o.name.lower(), o.raw_title.lower())
        if key not in seen:
            seen.add(key)
            out.append(o)
    return out
