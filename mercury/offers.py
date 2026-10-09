"""Offer routing and the Writer's per-prospect offer brief.

Routing is deterministic. At write time each prospect gets the first offer,
in the order ``offers:`` lists them, whose rule matches: its market (an
``icp.markets`` name, matched on the company's location the way the Writer
picks a language), its segment (the prospect's or company's industry) and
its observed signals (``signals.require`` / ``signals.exclude``, read like a
cohort: the newest observation per signal, and only a positive one counts).
No match falls back to the offer marked ``default``. An offer without any
rule is never chosen except as that default, so configs written only for
the demo gate, and configs without offers, route nothing and write exactly
as before.

The brief is what the Writer gets instead of "everything we sell": the
selected offer's approved content, the call to action and angle for the
step being written, verified facts from observations, aggregate evidence
only above its configured sample size, the case studies in scope for this
prospect, an optional confirmed pain (supplied by a hook, never made up)
and the offer's claim restrictions. No other offer's content is rendered.

Case studies are enforced twice: an out-of-scope one never enters the
prompt, and ``case_study_hits`` lets the Writer discard and the pre-send
gate block a draft that names one anyway.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("mercury.offers")


# ── Config helpers ──

def offers_of(config) -> list:
    return list(getattr(config, "offers", None) or [])


def routing_enabled(config) -> bool:
    """Routing does anything only once some offer has a rule or is the default."""
    return any(o.has_rule or o.default for o in offers_of(config))


def default_offer(config):
    return next((o for o in offers_of(config) if o.default), None)


def offer_by_key(config, key: str):
    key = (key or "").strip().lower()
    return next((o for o in offers_of(config) if o.key == key), None) if key else None


def market_for(config, company) -> str:
    """The icp.markets name a company belongs to ('' when none), matched on
    its location and domain like the Writer's language choice."""
    loc = ((getattr(company, "location", "") or "") + " "
           + (getattr(company, "domain", "") or "")).lower()
    for market in getattr(getattr(config, "icp", None), "markets", None) or []:
        if any(place.lower() in loc for place in market.places):
            return market.name.strip().lower()
    return ""


def segment_for(prospect, company=None) -> str:
    return ((getattr(prospect, "industry", "") or "")
            or (getattr(company, "industry", "") or "")).strip().lower()


def is_positive(observation: dict) -> bool:
    """A cohort's reading: a text signal's row is the finding, a number or
    boolean counts unless it is 0 ("checked, not there")."""
    value = observation.get("value_num")
    return value is None or value != 0


# ── Routing ──

@dataclass
class RoutingContext:
    """What routing knows about one prospect."""
    market: str = ""
    segment: str = ""
    # Newest observation per signal code for the prospect's company.
    observations: dict[str, dict] = field(default_factory=dict)
    company: object | None = None

    @property
    def signals(self) -> set[str]:
        return {code for code, obs in self.observations.items() if is_positive(obs)}


@dataclass
class OfferCheck:
    key: str
    matched: bool
    why: str


@dataclass
class RouteDecision:
    """The offer chosen for a prospect and why. ``offer`` None: no offer,
    the Writer works as it did before offers existed."""
    offer: object | None
    reason: str
    is_default: bool = False
    checks: list[OfferCheck] = field(default_factory=list)
    context: RoutingContext | None = None

    @property
    def key(self) -> str:
        return self.offer.key if self.offer is not None else ""

    def as_dict(self) -> dict:
        ctx = self.context or RoutingContext()
        return {
            "offer_key": self.key,
            "label": self.offer.label if self.offer is not None else "",
            "reason": self.reason,
            "is_default": self.is_default,
            "market": ctx.market,
            "segment": ctx.segment,
            "signals": sorted(ctx.signals),
            "checks": [{"offer_key": c.key, "matched": c.matched, "why": c.why} for c in self.checks],
        }


def evaluate(offer, ctx: RoutingContext) -> OfferCheck:
    """Whether one offer's rule matches, with every part of the reason."""
    if not offer.has_rule:
        why = "default offer, used when no rule matches" if offer.default else "no rule"
        return OfferCheck(offer.key, False, why)
    met, failed = [], []
    if offer.markets:
        if ctx.market in offer.markets:
            met.append(f"market {ctx.market}")
        else:
            failed.append(f"market is {ctx.market or 'unknown'}, needs {' or '.join(offer.markets)}")
    if offer.segments:
        if ctx.segment in offer.segments:
            met.append(f"segment {ctx.segment}")
        else:
            failed.append(f"segment is {ctx.segment or 'unknown'}, needs {' or '.join(offer.segments)}")
    signals = ctx.signals
    missing = [c for c in offer.signals.require if c not in signals]
    present = [c for c in offer.signals.exclude if c in signals]
    if offer.signals.require:
        if missing:
            failed.append(f"lacks {', '.join(missing)}")
        else:
            met.append(f"has {', '.join(offer.signals.require)}")
    if offer.signals.exclude:
        if present:
            failed.append(f"excluded by {', '.join(present)}")
        else:
            met.append(f"none of {', '.join(offer.signals.exclude)}")
    if failed:
        return OfferCheck(offer.key, False, "; ".join(failed))
    return OfferCheck(offer.key, True, ", ".join(met))


def route(offers: list, ctx: RoutingContext) -> RouteDecision:
    """First offer whose rule matches, else the default, else none."""
    checks = []
    for offer in offers:
        check = evaluate(offer, ctx)
        checks.append(check)
        if check.matched:
            return RouteDecision(offer, f"{offer.key} rule matched: {check.why}",
                                 checks=checks, context=ctx)
    fallback = next((o for o in offers if o.default), None)
    if fallback is not None:
        return RouteDecision(fallback, f"no offer rule matched; {fallback.key} is the default",
                             is_default=True, checks=checks, context=ctx)
    reason = ("no offer rule matched and no offer is the default"
              if any(o.has_rule for o in offers) else "no offer has a routing rule")
    return RouteDecision(None, reason, checks=checks, context=ctx)


def kept(offer, reason: str, ctx: RoutingContext | None = None) -> RouteDecision:
    """A decision already made: the offer an email or campaign carries."""
    return RouteDecision(offer, reason, context=ctx)


async def routing_context(state, config, prospect, company=None) -> RoutingContext:
    if company is None and getattr(prospect, "company_id", ""):
        try:
            company = await state.get_company(prospect.company_id)
        except Exception:
            company = None
    observations = {}
    if company is not None and getattr(company, "id", ""):
        observations = await state.company_signals(company.id)
    return RoutingContext(market=market_for(config, company), segment=segment_for(prospect, company),
                          observations=observations, company=company)


async def route_prospect(state, config, prospect) -> RouteDecision:
    ctx = await routing_context(state, config, prospect)
    if not routing_enabled(config):
        return RouteDecision(None, "no offer has a routing rule", context=ctx)
    return route(offers_of(config), ctx)


async def decision_for_key(state, config, prospect, offer_key: str, reason: str = "") -> RouteDecision:
    """The decision an existing email carries, without routing again: a
    regenerated draft keeps the offer its campaign was written for."""
    ctx = await routing_context(state, config, prospect)
    offer = offer_by_key(config, offer_key)
    if offer is None:
        why = f"{offer_key} is not in offers" if offer_key else "written without an offer"
        return RouteDecision(None, why, context=ctx)
    return kept(offer, reason or f"kept from the campaign ({offer.key})", ctx)


# ── Case studies ──

def in_scope(case_study, ctx: RoutingContext) -> bool:
    scope = case_study.scope
    if scope.markets and ctx.market not in scope.markets:
        return False
    if scope.segments and ctx.segment not in scope.segments:
        return False
    return True


def allowed_case_studies(config, offer, contexts: list[RoutingContext]) -> list:
    """The case studies a draft may cite: listed on the selected offer and
    in scope for every prospect it is written for. Without an offer, any
    configured case study in scope (nothing becomes newly forbidden)."""
    pool = offer.case_studies if offer is not None else [
        cs for o in offers_of(config) for cs in o.case_studies]
    return [cs for cs in pool if all(in_scope(cs, ctx) for ctx in contexts)]


def blocked_terms(config, offer, contexts: list[RoutingContext]) -> list[str]:
    """Every configured case-study name or alias a draft must not contain."""
    allowed = {t.lower() for cs in allowed_case_studies(config, offer, contexts) for t in cs.terms()}
    terms: dict[str, str] = {}
    for o in offers_of(config):
        for cs in o.case_studies:
            for term in cs.terms():
                if term.lower() not in allowed:
                    terms.setdefault(term.lower(), term)
    return list(terms.values())


def case_study_hits(text: str, terms: list[str]) -> list[str]:
    """The terms that appear in ``text`` as whole words, case-insensitively."""
    hits = []
    for term in terms or []:
        if re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text or "", re.IGNORECASE):
            hits.append(term)
    return hits


def has_case_studies(config) -> bool:
    return any(o.case_studies for o in offers_of(config))


async def blocked_references(state, config, item: dict, prospect) -> list[str]:
    """For the pre-send gate: the case-study names this email must not
    contain, given the offer it carries and the prospect's market and
    segment. Fails closed: if it cannot work them out, every configured
    case study is blocked."""
    if config is None or not has_case_studies(config):
        return []
    try:
        offer = offer_by_key(config, await state.outbox_offer_key(item))
        ctx = await routing_context(state, config, prospect) if prospect is not None else RoutingContext()
        return blocked_terms(config, offer, [ctx])
    except Exception as exc:
        logger.warning(f"Offers: could not scope case studies for outbox row {item.get('id')}: {exc}")
        return [t for o in offers_of(config) for cs in o.case_studies for t in cs.terms()]


# ── The brief ──

@dataclass
class ConfirmedPain:
    """One pain a human confirmed (#59 supplies these). The brief names it
    as the only pain the email may use."""
    code: str
    words: str
    scene: str = ""
    cost: str = ""


def _number(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def fact_line(observation: dict) -> str:
    label = observation.get("label") or observation.get("signal_code", "")
    num, text = observation.get("value_num"), (observation.get("value_text") or "").strip()
    when = str(observation.get("observed_at") or "")[:10]
    seen = f" (observed {when})" if when else ""
    if observation.get("value_type") == "bool":
        # A true flag reads as the statement itself ("No website at all"),
        # not "No website at all: yes", in the brief and on the review desk.
        return f"- {label}{seen}" if is_positive(observation) else f"- {label}: no{seen}"
    if num is not None:
        value = _number(num) + (f" ({text})" if text else "")
    else:
        value = text or "yes"
    return f"- {label}: {value}{seen}"


@dataclass
class OfferBrief:
    offer: object
    steps: list[int]
    reason: str = ""
    pain: ConfirmedPain | None = None
    facts: list[str] = field(default_factory=list)
    evidence: str = ""
    case_studies: list = field(default_factory=list)
    # Case-study names and aliases the draft must not contain. Never rendered.
    blocked: list[str] = field(default_factory=list)

    @property
    def authoritative(self) -> bool:
        """With a summary, the brief replaces the product description."""
        return bool(self.offer.content.summary.strip())

    def step(self, n: int):
        return self.offer.steps.get(n)

    def render(self) -> str:
        offer, content = self.offer, self.offer.content
        many = len(self.steps) > 1
        lines = ["\n\nOFFER BRIEF",
                 f"This is the only offer for {'these emails' if many else 'this email'}. "
                 + ("It replaces any product or offer description above. " if self.authoritative else "")
                 + "Never mention another offer, product or service."]
        lines.append(f"Offer: {offer.label}")
        if content.summary.strip():
            lines.append(f"What it is: {content.summary.strip()}")
        if content.claims:
            lines.append("Approved claims (claim nothing else about the offer):")
            lines += [f"- {c}" for c in content.claims]
        if self.pain is not None:
            lines.append(f"Confirmed pain (the only pain you may name): {self.pain.words}")
            if self.pain.scene:
                lines.append(f"- Scene: {self.pain.scene}")
            if self.pain.cost:
                lines.append(f"- What it costs them: {self.pain.cost}")
        else:
            lines.append("Confirmed pain: none supplied. Do not state a pain as a fact about "
                         "this business; you may ask how they handle what the offer addresses.")
        if self.facts:
            lines.append("Verified facts from Mercury's observations (use as written, never round "
                         "or embellish):")
            lines += self.facts
        if self.evidence:
            lines.append("Aggregate evidence, computed from Mercury's own observations (quote it "
                         "exactly or not at all):")
            lines.append(f"- {self.evidence}")
        if self.case_studies:
            lines.append("Case studies you may cite (only these, only in these words):")
            lines += [f"- {cs.name}" + (f": {cs.summary}" if cs.summary else "") for cs in self.case_studies]
        else:
            lines.append("Case studies: none for this prospect. Do not name a client or describe "
                         "a client's result.")
        step_lines = []
        for n in self.steps:
            step = self.step(n)
            if step is None or not (step.cta or step.angle):
                continue
            parts = []
            if step.angle:
                parts.append(f"angle: {step.angle}")
            if step.cta:
                parts.append(f"call to action: {step.cta}")
            step_lines.append(f"- Email {n}: " + "; ".join(parts))
        if step_lines:
            lines.append("Each email's angle and call to action (the call to action is its one "
                         "question or ask):" if many else "This email's angle and call to action "
                         "(the call to action is its one question or ask):")
            lines += step_lines
        lines.append("Claim restrictions:")
        lines += [f"- {r}" for r in offer.restrictions]
        lines.append("- Never invent statistics, results, clients or case studies. Every number "
                     "you quote must appear in this brief or the FACTS.")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        return {
            "offer_key": self.offer.key,
            "label": self.offer.label,
            "steps": self.steps,
            "reason": self.reason,
            "authoritative": self.authoritative,
            "pain": None if self.pain is None else self.pain.__dict__,
            "facts": self.facts,
            "evidence": self.evidence,
            "case_studies": [cs.name for cs in self.case_studies],
        }


async def aggregate_evidence(state, offer, contexts: list[RoutingContext], steps: list[int]) -> str:
    """The configured statistic, only when relevant and above its sample size."""
    ev = offer.evidence
    if ev is None:
        return ""
    if ev.steps and not set(ev.steps) & set(steps):
        return ""
    if ev.segments and not all(ctx.segment in ev.segments for ctx in contexts):
        return ""
    matched, checked = await state.cohort_share(ev.require, ev.exclude, ev.segments)
    if checked < ev.min_sample or matched <= 0:
        return ""
    share = round(100 * matched / checked)
    where = f" in {' or '.join(ev.segments)}" if ev.segments else ""
    line = f"Of {checked} businesses{where} Mercury checked, {matched} ({share}%) {ev.description.strip()}"
    line = line.rstrip(".") + "."
    if ev.steps and len(steps) > 1:
        line += f" Use it only in email {' or '.join(str(s) for s in ev.steps)}."
    return line


async def build_brief(state, config, decision: RouteDecision, steps: list[int],
                      contexts: list[RoutingContext], pain: ConfirmedPain | None = None) -> OfferBrief | None:
    """The brief for one decision. Per-prospect facts only when written for
    one prospect (a shared sequence template has merge variables instead)."""
    offer = decision.offer
    if offer is None:
        return None
    facts = []
    if len(contexts) == 1:
        codes = offer.facts or offer.signals.require
        observations = contexts[0].observations
        facts = [fact_line(observations[c]) for c in codes if c in observations]
    return OfferBrief(
        offer=offer, steps=list(steps), reason=decision.reason, pain=pain, facts=facts,
        evidence=await aggregate_evidence(state, offer, contexts, list(steps)),
        case_studies=allowed_case_studies(config, offer, contexts),
        blocked=blocked_terms(config, offer, contexts),
    )


# ── Checks and the Outbox ──

def check_offers(config, vocabulary: dict[str, str]) -> list[str]:
    """Plain warnings about an offers config, given the signal vocabulary
    ({code: status}). A rule over a code that is unknown or not confirmed
    can never match, since nothing collects it."""
    offers = offers_of(config)
    problems = []
    enabled = routing_enabled(config)
    for offer in offers:
        for code in sorted(offer.signal_codes()):
            status = vocabulary.get(code)
            if status is None:
                problems.append(f"{offer.key}: signal {code} is not in the signal vocabulary "
                                "(`mercury signals`), so it is never observed.")
            elif status != "confirmed":
                problems.append(f"{offer.key}: signal {code} is {status}, so Mercury does not "
                                f"collect it (mercury signals --confirm {code}).")
        if enabled and not offer.has_rule and not offer.default:
            problems.append(f"{offer.key}: no markets, segments or signals and not the default, "
                            "so routing never picks it.")
    if enabled and default_offer(config) is None:
        problems.append("No offer is the default: a prospect no rule matches is written without "
                        "an offer, as before offers existed.")
    return problems


async def offer_problems(state, config) -> list[str]:
    vocabulary = {r["code"]: r["status"] for r in await state.get_signal_codes()}
    return check_offers(config, vocabulary)


async def annotate_outbox(state, config, rows: list[dict]) -> list[dict]:
    """Give each row its effective ``offer_key`` (its own, else its
    campaign's) and ``offer``: None, or {key, label, reason, is_default,
    configured}. ``reason`` is the routing decision recorded at write time."""
    if not rows:
        return rows
    info = await state.outbox_offers(rows)
    for row in rows:
        found = info.get(row.get("id")) or {}
        key = found.get("offer_key") or ""
        row["offer_key"] = key
        if not key:
            row["offer"] = None
            continue
        offer = offer_by_key(config, key) if config is not None else None
        row["offer"] = {
            "key": key,
            "label": offer.label if offer is not None else key,
            "reason": found.get("reason") or "",
            "is_default": bool(found.get("is_default")),
            "configured": offer is not None,
        }
    return rows
