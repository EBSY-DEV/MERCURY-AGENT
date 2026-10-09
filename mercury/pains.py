"""The pain library: what a cold email may say the prospect is struggling with.

Governed the way signals are. Mercury (the trainer, or a person) PROPOSES a
pain; a person confirms or rejects it; only confirmed pains are ever written
from. A rejected pain stays on file as the never-use list, so retraining
cannot bring it back and a draft that mentions it is stopped in code.

This module is everything the Writer and the send gate call:

* ``propose_pains``        the trainer's entry point (never confirms, never
                           resurrects)
* ``select_pain`` /        exactly one confirmed pain for a prospect and
  ``select_pain_for_prospect``  offer, or none, plus the never-use list
* ``pain_prompt_blocks``   the selection as prompt text
* ``find_rejected_pain_hits``  the deterministic guard over a draft

How a proposal is matched to a pain already on file (``same_pain``). Two
statements are the same pain when ANY of these holds, tried against the
code, the original wording, the label and the owner's words of each pain:

1. they derive the same code (the code is a hash of the normalised text);
2. their normalised text is equal (lower case, accents and punctuation
   stripped, whitespace collapsed);
3. their content words (stop words dropped, plural/-ing/-ed suffixes
   trimmed) overlap strongly: Jaccard >= 0.5, or at least 70% of the
   shorter statement's words (when it has 3 or more) appear in the other.

A match against a rejected pain drops the proposal. A match against a
proposed or confirmed pain leaves that pain as it is. Anything else is
inserted as 'proposed'. The rule leans towards dropping: a pain it wrongly
drops costs one `mercury pains add`, a rejected pain it lets back costs a
bad email. A rewording too loose to match is still caught at send time by
the word guard.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable

CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,47}$")
TRAINER = "trainer"

_STOPWORDS = frozenset("""
a about after again all also am an and any are as at be because been before being
both but by can cannot could did do does doing done down during each few for from
get gets got had has have having he her here hers him his how i if in into is it
its just like make makes many may me more most much must my no nor not now of off
on once one only or other our out over own really same she should so some such
than that the their them then there these they this those through to too under
until up us use used very want was we were what when where which while who whom
why will with within without would you your yours
""".split())


# ── Text ──


def normalize_text(text: str) -> str:
    """Lower case, accents removed, punctuation gone, whitespace collapsed."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _stem(word: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            return word[: -len(suffix)]
    return word


def content_words(text: str) -> frozenset[str]:
    """The words that carry a statement's meaning, lightly stemmed."""
    return frozenset(_stem(w) for w in normalize_text(text).split()
                     if len(w) >= 3 and w not in _STOPWORDS)


def derive_code(text: str) -> str:
    """A stable code for a statement: a few of its words and a hash of its
    normalised text, so proposing the same text twice yields the same code."""
    norm = normalize_text(text)
    digest = hashlib.sha1(norm.encode("utf-8")).hexdigest()[:6].upper()
    words = [w.upper() for w in norm.split() if w not in _STOPWORDS and len(w) >= 3][:3]
    return "_".join(["PAIN", *words, digest])[:48].rstrip("_")


def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (code or "").upper()).strip("_")


def same_pain(a: str, b: str) -> bool:
    """Whether two statements are the same complaint (rule 2 and 3 above)."""
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    wa, wb = content_words(a), content_words(b)
    if not wa or not wb:
        return False
    shared = len(wa & wb)
    if shared / len(wa | wb) >= 0.5:
        return True
    smaller = min(len(wa), len(wb))
    return smaller >= 3 and shared / smaller >= 0.7


def _statements(pain: dict) -> list[str]:
    return [t for t in (pain.get("origin_text"), pain.get("label"), pain.get("owner_words")) if t]


def find_match(text: str, pains: Iterable[dict], code: str = "") -> dict | None:
    """The pain on file that ``text`` (or ``code``) repeats, if any."""
    code = code or derive_code(text)
    for pain in pains:
        if pain["code"] == code or any(same_pain(text, s) for s in _statements(pain)):
            return pain
    return None


# ── The trainer proposes ──


def _trainer_statement(item) -> dict | None:
    """One ``pain_points`` entry as a proposal. The trainer returns plain
    strings; a dict with label/scene/cost fields is accepted too."""
    if isinstance(item, str):
        label, extra = item.strip(), {}
    elif isinstance(item, dict):
        label = str(item.get("label") or item.get("pain") or item.get("text") or "").strip()
        extra = {k: str(item[k]).strip() for k in ("scene", "cost", "sector", "market")
                 if item.get(k)}
    else:
        return None
    if len(content_words(label)) < 2:
        return None
    return {"label": label[:200], **extra}


async def propose_pains(state, statements: Iterable, *, evidence: str = "",
                        source: str = TRAINER) -> list[dict]:
    """Record pain statements as 'proposed' unless they are already on file.

    Never confirms anything, never changes a pain that exists, and never
    brings back a rejected one (see the module docstring for the match
    rule). Returns one result per usable statement:
    ``{"label", "code", "outcome", "matched"}`` with outcome ``proposed``,
    ``exists`` (matched a proposed/confirmed pain) or ``rejected`` (matched
    a rejected pain, so dropped)."""
    known = await state.list_pains()
    results = []
    for item in statements or []:
        proposal = _trainer_statement(item)
        if proposal is None:
            continue
        label = proposal["label"]
        match = find_match(label, known)
        if match:
            outcome = "rejected" if match["status"] == "rejected" else "exists"
            results.append({"label": label, "code": match["code"], "outcome": outcome,
                            "matched": match["code"]})
            continue
        code = derive_code(label)
        added = await state.add_pain(
            code, label=label, scene=proposal.get("scene", ""), cost=proposal.get("cost", ""),
            sector=proposal.get("sector", ""), market=proposal.get("market", ""),
            evidence=[evidence] if evidence else [], origin_text=label,
            source=source, status="proposed")
        if added:
            known = await state.list_pains()
        results.append({"label": label, "code": code, "outcome": "proposed" if added else "exists",
                        "matched": "" if added else code})
    return results


async def confirmed_pain_lines(state) -> list[str]:
    """The confirmed pains as one line each, for the knowledge file the
    Writer reads. Nothing proposed or rejected is ever in it."""
    return [p["owner_words"].strip() or p["label"].strip()
            for p in await state.list_pains(status="confirmed")
            if (p["owner_words"] or p["label"]).strip()]


# ── Selection ──


def _tokens(text: str) -> frozenset[str]:
    return frozenset(normalize_text(text).split())


def _sector_matches(pain_sector: str, sector: str) -> bool:
    """A pain with no sector fits anyone. Otherwise one side's words must
    all appear in the other's ("hvac" fits "HVAC contractors")."""
    if not pain_sector.strip():
        return True
    a, b = _tokens(pain_sector), _tokens(sector)
    return bool(a and b and (a <= b or b <= a))


@dataclass(frozen=True)
class PainSelection:
    """The pain chosen for one email, and what it must not say."""
    pain: dict | None
    reason: str
    rejected: tuple[dict, ...] = ()
    considered: tuple[str, ...] = field(default=(), compare=False)

    @property
    def code(self) -> str:
        """What to store as ``outbox.pain_code`` ('' = no pain)."""
        return self.pain["code"] if self.pain else ""


def choose_pain(confirmed: Iterable[dict], *, sector: str = "", market: str = "",
                offer_key: str = "", signal_codes: Iterable[str] = ()) -> tuple[dict | None, str]:
    """Pick one pain from ``confirmed``, or none. Pure and deterministic.

    A pain is eligible when it is confirmed and
    * its offer is empty or the selected offer (an offer-bound pain never
      goes into an email for another offer, or for none),
    * its market is empty or the prospect's market,
    * its sector is empty or fits the prospect's sector,
    * its signal codes are empty, or the prospect carries at least one.

    Among the eligible, the most specific wins: more matched signals, then a
    named sector, then a named market, then a named offer, then the lowest
    code. The same inputs always give the same pain."""
    signals = {s.strip().upper() for s in signal_codes if s}
    market, offer_key = (market or "").strip().lower(), (offer_key or "").strip().lower()
    ranked = []
    for pain in confirmed:
        if pain.get("status") != "confirmed":
            continue
        if pain["offer_key"] and pain["offer_key"] != offer_key:
            continue
        if pain["market"] and pain["market"] != market:
            continue
        if not _sector_matches(pain["sector"], sector):
            continue
        wanted = {s.upper() for s in pain["signal_codes"]}
        matched = wanted & signals
        if wanted and not matched:
            continue
        ranked.append(((-len(matched), 0 if pain["sector"] else 1, 0 if pain["market"] else 1,
                        0 if pain["offer_key"] else 1, pain["code"]), pain, sorted(matched)))
    if not ranked:
        return None, "no confirmed pain fits this sector, market, offer and signals"
    _, pain, matched = min(ranked, key=lambda r: r[0])
    parts = [f"signals {', '.join(matched)}" if matched else "no signal required"]
    for name in ("sector", "market", "offer_key"):
        if pain[name]:
            parts.append(f"{name.split('_')[0]} {pain[name]}")
    more = len(ranked) - 1
    return pain, "matched " + "; ".join(parts) + (f" (best of {len(ranked)})" if more else "")


async def select_pain(state, *, sector: str = "", market: str = "", offer_key: str = "",
                      signal_codes: Iterable[str] = ()) -> PainSelection:
    """The Writer's call when it already knows the facts."""
    pains = await state.list_pains()
    pain, reason = choose_pain([p for p in pains if p["status"] == "confirmed"], sector=sector,
                               market=market, offer_key=offer_key, signal_codes=signal_codes)
    return PainSelection(pain=pain, reason=reason,
                         rejected=tuple(p for p in pains if p["status"] == "rejected"))


def market_for_company(config, company) -> str:
    """The icp.markets entry a company belongs to, by its location (the same
    place match the Writer uses for language). '' when none matches."""
    markets = getattr(getattr(config, "icp", None), "markets", None) or []
    loc = ((getattr(company, "location", "") or "") + " " + (getattr(company, "domain", "") or "")).lower()
    for market in markets:
        if any(place.lower() in loc for place in market.places):
            return market.name.strip().lower()
    return ""


async def select_pain_for_prospect(state, prospect, company=None, *, config=None,
                                   offer_key: str = "") -> PainSelection:
    """The Writer's call: the one confirmed pain for this prospect and offer.

    Sector comes from the company's industry (else the prospect's), market
    from ``config.icp.markets`` by the company's location, and signals from
    the company's current CONFIRMED observations."""
    if company is None and getattr(prospect, "company_id", ""):
        company = await state.get_company(prospect.company_id)
    sector = (getattr(company, "industry", "") or getattr(prospect, "industry", "") or "")
    signals = await state.company_signal_codes(getattr(company, "id", "") or "")
    return await select_pain(state, sector=sector, market=market_for_company(config, company),
                             offer_key=offer_key, signal_codes=signals)


# ── Prompt text ──


def _pain_lines(pain: dict) -> list[str]:
    lines = []
    if pain.get("owner_words"):
        lines.append(f'- In their own words: "{pain["owner_words"].strip()}"')
    elif pain.get("label"):
        lines.append(f"- The pain: {pain['label'].strip()}")
    if pain.get("scene"):
        lines.append(f"- The scene: {pain['scene'].strip()}")
    if pain.get("cost"):
        lines.append(f"- What it costs them: {pain['cost'].strip()}")
    return lines


def _reference_line(pain: dict) -> list[str]:
    """The pain's code, so the recorded prompt says which pain it was written
    around. Marked internal: it is never part of the email."""
    if not pain.get("code"):
        return []
    return [f"- Pain reference (internal, never write it in the email): {pain['code']}"]


def pain_block(selection: PainSelection) -> str:
    """The 'use only this pain' block. With no pain it says so, so the model
    is not left to invent one."""
    if selection.pain is None:
        return ("PAIN: none confirmed for this prospect. Do not state, imply or invent any "
                "problem the prospect has. Write only from the verified facts.")
    return "\n".join([
        "PAIN (the only pain you may raise in this email; do not add, combine or invent others):",
        *_pain_lines(selection.pain),
        *_reference_line(selection.pain),
    ])


def never_use_block(rejected: Iterable[dict]) -> str:
    """The 'never use these' block, '' when nothing is rejected."""
    entries = []
    for pain in rejected:
        text = (pain.get("owner_words") or pain.get("label") or "").strip()
        if text:
            entries.append(f"- {text}")
    if not entries:
        return ""
    return ("NEVER raise these (a person rejected them; an email that touches any of them is "
            "discarded):\n" + "\n".join(entries))


def pain_prompt_blocks(selection: PainSelection) -> str:
    """Both blocks, ready to append to the Writer's prompt."""
    return "\n\n".join(b for b in (pain_block(selection), never_use_block(selection.rejected)) if b)


# ── The Outbox ──


async def annotate_outbox(state, rows: list[dict]) -> list[dict]:
    """Give each outbox row ``pain``: None when it was written around no
    pain, else {code, label, words, scene, cost, status}. ``words`` is the
    owner's own wording the Writer was given (the label when there are none),
    read from the pain library as it stands now; ``status`` is that pain's
    current state, ``missing`` if it has since been removed."""
    wanted = {r.get("pain_code") for r in rows if r.get("pain_code")}
    library = {p["code"]: p for p in await state.list_pains()} if wanted else {}
    for row in rows:
        code = row.get("pain_code") or ""
        pain = library.get(code)
        if not code:
            row["pain"] = None
        elif pain is None:
            row["pain"] = {"code": code, "label": "", "words": "", "scene": "", "cost": "",
                           "status": "missing"}
        else:
            row["pain"] = {"code": code, "label": pain["label"],
                           "words": (pain["owner_words"] or pain["label"]).strip(),
                           "scene": pain["scene"], "cost": pain["cost"], "status": pain["status"]}
    return rows


# ── The guard ──


@dataclass(frozen=True)
class PainHit:
    code: str
    words: tuple[str, ...]
    reason: str


def distinctive_words(pain: dict, others: Iterable[dict] = ()) -> frozenset[str]:
    """The words that mark a text as using ``pain``: its content words, minus
    any that a pain in ``others`` also uses (so a rejected pain cannot make a
    confirmed pain's own vocabulary unsendable)."""
    own = content_words(" ".join(t for t in (pain.get("label"), pain.get("owner_words"),
                                               pain.get("scene")) if t))
    shared: set[str] = set()
    for other in others:
        shared |= content_words(" ".join(t for t in (other.get("label"), other.get("owner_words"),
                                                      other.get("scene")) if t))
    return frozenset(w for w in own - shared if len(w) >= 4)


def find_rejected_pain_hits(text: str, rejected: Iterable[dict], allowed: Iterable[dict] = ()) -> list[PainHit]:
    """Which rejected pains a draft appears to raise. Deterministic, no model.

    A pain is hit when
    * one of its ``avoid_terms`` appears in the text as a whole phrase, or
    * at least two of its distinctive words appear (one, if it has only
      one). Distinctive words are the pain's content words of 4+ letters
      that no pain in ``allowed`` also uses; pass the pain chosen for the
      email (or all confirmed pains) as ``allowed``.

    Words are compared after the same normalisation and trimming as pain
    matching, so "missed"/"misses" and plurals line up."""
    haystack = f" {normalize_text(text)} "
    drafted = content_words(text)
    allowed = list(allowed)
    hits = []
    for pain in rejected:
        phrases = [p for p in pain.get("avoid_terms") or [] if normalize_text(p)]
        phrase_hit = next((p for p in phrases if f" {normalize_text(p)} " in haystack), None)
        if phrase_hit:
            hits.append(PainHit(pain["code"], (phrase_hit,), f'uses the avoided phrase "{phrase_hit}"'))
            continue
        words = distinctive_words(pain, allowed)
        found = sorted(words & drafted)
        if found and len(found) >= min(2, len(words)):
            hits.append(PainHit(pain["code"], tuple(found),
                                f"uses words of a rejected pain: {', '.join(found[:4])}"))
    return hits
