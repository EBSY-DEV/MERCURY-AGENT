"""The registry provider interface, and the name/place normalization every
provider shares.

A public business registry answers one question: "who is behind this
business?". A provider knows how to search one registry and read one entity's
page. Everything else (eligibility, matching, abstaining, caching, writing
observations) lives in ``service.py`` and is the same for every registry, so
adding a state is one small class.
"""

from __future__ import annotations

import re
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class RegistryError(Exception):
    """A lookup could not be completed. Never a "no match": that is an answer."""


class RegistryUnavailable(RegistryError):
    """The registry refused or could not serve us (bot challenge, 5xx, timeout).

    Not cached as a result: the business was never actually looked up.
    """


@dataclass
class EntityCandidate:
    """One row of a name search."""
    name: str
    document_number: str
    status: str                 # as the registry words it: "Active", "Inactive"
    detail_url: str

    @property
    def is_active(self) -> bool:
        return self.status.strip().lower() in ("active", "act")


@dataclass
class Officer:
    name: str                   # "Jane Doe": display order, title-cased
    title: str                  # "Manager"
    raw_title: str = ""         # the registry's own code or wording
    is_person: bool = True      # False for an entity acting as officer


@dataclass
class EntityDetail:
    name: str
    document_number: str
    status: str
    detail_url: str
    principal_city: str = ""
    mailing_city: str = ""
    officers: list[Officer] = field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.status.strip().lower() in ("active", "act")


class RegistryProvider(ABC):
    """One public registry, for one jurisdiction."""

    key = ""            # stable id stored with every lookup: "fl_sunbiz"
    jurisdiction = ""   # two-letter US state code this registry covers
    label = ""          # for people: "Florida Division of Corporations"

    def supports(self, location: str) -> bool:
        """Eligibility comes from the registry's own jurisdiction, never from
        campaign targeting: a company is eligible when it is located here."""
        return location_state(location) == self.jurisdiction

    @abstractmethod
    async def search(self, name: str) -> list[EntityCandidate]:
        """Entities whose registered name is near ``name``."""

    @abstractmethod
    async def detail(self, candidate: EntityCandidate) -> EntityDetail:
        """The entity's page: status, addresses, officers."""

    async def aclose(self) -> None:
        return None


# ── Normalization ──

LEGAL_SUFFIXES = frozenset({
    "llc", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llp", "lp", "pllc", "pa", "pc", "plc", "lc", "plc",
    "chartered", "chtd", "pllp",
})
_LOCATION_SPLIT = re.compile(r"\s+[-–—|]\s+|\s*\|\s*")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text or "")
                   if not unicodedata.combining(c))


def _strip_name_decorations(name: str, *, strip_location_suffix: bool = True) -> str:
    """A business name with its branch tail, trailing parentheses and
    trailing legal suffixes removed, in its original case. The one place
    that knows what a legal suffix or a location tail is: both the
    comparison key below and ``short_business_name`` build on it."""
    text = (name or "").strip()
    if strip_location_suffix:
        head = _LOCATION_SPLIT.split(text, maxsplit=1)[0].strip()
        text = head or text
    text = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip() or text
    tokens = text.split()
    while len(tokens) > 1 and re.sub(r"[^a-z0-9]", "", _fold(tokens[-1]).lower()) in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens).rstrip(" ,;:-").strip() or text


def short_business_name(name: str) -> str:
    """The name to use in a subject and after the first mention: the
    business name without legal suffixes (LLC, Inc, Corp, Co, Ltd, PLLC, ...)
    or a location after a dash or pipe, keeping its own capitalisation.
    "Acme Roofing LLC - Springfield" becomes "Acme Roofing"."""
    short = _strip_name_decorations(name)
    # "Smith & Sons Co." leaves "Smith & Sons"; "Smith &" would be wrong.
    return re.sub(r"\s*(?:&|\+|and)$", "", short, flags=re.IGNORECASE).strip() or short


def name_variants(name: str) -> list[str]:
    """Other spellings of a full business name a draft may use for it: the
    name without its location tail but with its legal suffix kept."""
    head = _LOCATION_SPLIT.split((name or "").strip(), maxsplit=1)[0].strip()
    return [head] if head and head != (name or "").strip() else []


def normalize_business_name(name: str, *, strip_location_suffix: bool = True) -> str:
    """The comparison key for a business name.

    Lowercase and accent-free; "&" becomes "and"; punctuation, the word
    "the", and trailing legal suffixes (LLC, Inc, Corp, Co, ...) are dropped.
    With ``strip_location_suffix`` a branch tail after a spaced dash or a pipe
    ("Acme Roofing - Tampa") is cut off. That is for OUR side only: a name
    taken from the registry is already the legal name and is kept whole.
    """
    text = _fold(_strip_name_decorations(name, strip_location_suffix=strip_location_suffix))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[.']", "", text)            # l.l.c. -> llc, o'neil -> oneil
    text = re.sub(r"[^a-z0-9]+", " ", text)
    tokens = [t for t in text.split() if t != "the"]
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def normalize_city(city: str) -> str:
    text = _fold(city).lower()
    text = re.sub(r"\bsaint\b", "st", text)
    text = re.sub(r"\bft\b", "fort", text)
    text = re.sub(r"\bmt\b", "mount", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


US_STATES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN",
    "mississippi": "MS", "missouri": "MO", "montana": "MT", "nebraska": "NE",
    "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
    "district of columbia": "DC",
}
_STATE_CODES = set(US_STATES.values())


def location_state(location: str) -> str:
    """The US state a free-text location names, as a two-letter code, or "".

    Reads "Miami, FL", "Miami, Florida", "Miami FL 33101", "Tampa, FL, USA".
    A location with no state ("Santo Domingo") is "", never a guess.
    """
    text = _fold(location).strip()
    if not text:
        return ""
    parts = [p.strip() for p in re.split(r"[,;]", text) if p.strip()]
    # An explicit code wins over a name: "Washington, DC" is not Washington state.
    for part in parts[1:]:
        m = re.match(r"^([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?$", part)
        if m and m.group(1).upper() in _STATE_CODES:
            return m.group(1).upper()
    m = re.search(r"\b([A-Z]{2})\s+\d{5}(?:-\d{4})?\b", text) or \
        re.search(r"\s([A-Z]{2})$", text)
    if m and m.group(1) in _STATE_CODES:
        return m.group(1)
    low = text.lower()
    # Longest names first so "west virginia" is not read as "virginia".
    for name in sorted(US_STATES, key=len, reverse=True):
        if re.search(rf"(?<![a-z]){re.escape(name)}(?![a-z])", low):
            return US_STATES[name]
    return ""


def location_city(location: str) -> str:
    """The city part of "City, ST": the text before the first comma, unless
    that is itself only a state or a country."""
    text = _fold(location).strip()
    if not text:
        return ""
    head = text.split(",", 1)[0].strip()
    head = re.sub(r"\s+[A-Z]{2}\s*\d{0,5}(?:-\d{4})?$", "", head).strip()
    if not head:
        return ""
    if head.lower() in US_STATES or head.upper() in _STATE_CODES:
        # "Florida, USA" has no city; "Washington, DC" does.
        rest = [p.strip() for p in text.split(",")[1:] if p.strip()]
        if not (rest and rest[0].upper() in _STATE_CODES):
            return ""
    return head


_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "v", "esq", "md", "phd", "dds", "cpa"})
_ROMAN = frozenset({"II", "III", "IV"})


def display_name(raw: str) -> str:
    """"DOE, JANE Q" -> "Jane Q Doe"; "ROE, RICK W., IV" -> "Rick W. Roe IV"."""
    raw = re.sub(r"\s+", " ", (raw or "").replace("\xa0", " ").strip())
    if "," in raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        suffix = ""
        if len(parts) > 2 and parts[-1].strip(".").lower() in _NAME_SUFFIXES:
            suffix = parts.pop()
        raw = f"{' '.join(parts[1:])} {parts[0]} {suffix}".strip() if len(parts) > 1 else parts[0]
    return " ".join(_cap(w) for w in raw.split())


def _cap(word: str) -> str:
    if not word.isupper() and not word.islower():
        return word
    if word.strip(".") in _ROMAN:
        return word
    if len(word) <= 2 and "." in word:
        return word.upper()
    out = word.lower().title()
    out = re.sub(r"(?<=\bMc)([a-z])", lambda m: m.group(1).upper(), out)
    return out


_ACRONYMS = frozenset({"VP", "SVP", "EVP", "AVP", "CEO", "CFO", "COO", "CTO", "CMO",
                       "CIO", "CRO", "LLC", "MD", "HR", "IT"})


def tidy_title(raw: str) -> str:
    """"EXECUTIVE VP & CFO" -> "Executive VP & CFO". Mixed-case text is the
    registry's own wording and is left alone."""
    raw = re.sub(r"\s+", " ", (raw or "").replace("\xa0", " ")).strip()
    if not raw.isupper():
        return raw
    return " ".join(w if w.strip(",&") in _ACRONYMS or w == "&" else w.title()
                    for w in raw.split())
