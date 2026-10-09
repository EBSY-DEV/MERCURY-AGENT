"""What the Outbox review desk shows next to a draft.

Three read-only annotations on outbox rows, each a single batched query:

* ``contact``: who the email goes to (name, title, company, location and
  the address's verification status), from the prospect and its company.
* ``sequence``: the other steps of the same thread ({total, steps: [{id,
  step, status, send_at, sent_at}]}), so a reviewer sees what approving
  one email sets in motion. None for a reply.
* ``brief``: the parts of the offer brief the draft was written from:
  ``facts`` (the verified facts, as given to the Writer), ``asks_for``
  (this step's call to action) and ``kept_out`` (the offer's claim
  restrictions). Read from the prompt the generation recorded, so it is what
  the Writer actually saw; a row without a recorded brief falls back to the
  offer as configured now (no facts then: those are never reconstructed).
"""

from __future__ import annotations

import re

import aiosqlite

# The lines OfferBrief.render() writes (mercury/offers.py).
_BRIEF_START = "OFFER BRIEF"
_FACTS_HEAD = "Verified facts from Mercury's observations"
_RESTRICTIONS_HEAD = "Claim restrictions:"
# Every brief ends its restrictions with this one; it is not offer-specific.
_GENERIC_RESTRICTION = "Never invent statistics"
_STEP_LINE = re.compile(r"^- Email (\d+): (.*)$")
_CTA = re.compile(r"call to action: (.*)$")


def parse_brief(prompt: str, step: int) -> dict | None:
    """The brief parts of a recorded prompt, or None when it has no brief."""
    text = prompt or ""
    start = text.find(_BRIEF_START)
    if start < 0:
        return None
    facts, kept_out, asks_for, mode = [], [], "", ""
    for raw in text[start:].splitlines()[1:]:
        line = raw.strip()
        if line.startswith(_FACTS_HEAD):
            mode = "facts"
            continue
        if line.startswith(_RESTRICTIONS_HEAD):
            mode = "restrictions"
            continue
        match = _STEP_LINE.match(line)
        if match:
            mode = ""
            cta = _CTA.search(match.group(2))
            if int(match.group(1)) == int(step or 1) and cta:
                asks_for = cta.group(1).strip()
            continue
        if not line.startswith("- "):
            if mode == "restrictions":
                break  # the brief ends after its restrictions
            mode = ""
            continue
        item = line[2:].strip()
        if mode == "facts":
            facts.append(item)
        elif mode == "restrictions" and not item.startswith(_GENERIC_RESTRICTION):
            kept_out.append(item)
    return {"facts": facts, "asks_for": asks_for, "kept_out": kept_out, "source": "generation"}


def _configured_brief(config, offer_key: str, step: int) -> dict | None:
    from mercury.offers import offer_by_key

    offer = offer_by_key(config, offer_key) if config is not None and offer_key else None
    if offer is None:
        return None
    step_cfg = offer.steps.get(int(step or 1))
    return {"facts": [], "asks_for": (step_cfg.cta if step_cfg else "").strip(),
            "kept_out": [r for r in offer.restrictions if r.strip()], "source": "config"}


async def _fetch(state, sql: str, params) -> list[dict]:
    async with state._connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, tuple(params)) as cursor:
            return [dict(r) for r in await cursor.fetchall()]


def _chunks(values: list, size: int = 400):
    for i in range(0, len(values), size):
        yield values[i:i + size]


async def annotate_contacts(state, rows: list[dict]) -> list[dict]:
    ids = sorted({r.get("prospect_id") for r in rows if r.get("prospect_id")})
    found: dict[str, dict] = {}
    for chunk in _chunks(ids):
        for r in await _fetch(
                state,
                "SELECT p.id, p.first_name, p.last_name, p.title, p.email_status, "
                "COALESCE(NULLIF(c.name, ''), p.company, '') AS company, "
                "COALESCE(c.location, '') AS location, COALESCE(p.company_id, '') AS company_id "
                "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id "
                f"WHERE p.id IN ({', '.join('?' for _ in chunk)})", chunk):
            found[r["id"]] = r
    for row in rows:
        p = found.get(row.get("prospect_id") or "")
        if p is None:
            row["contact"] = None
            continue
        name = " ".join(x for x in ((p["first_name"] or "").strip(), (p["last_name"] or "").strip()) if x)
        row["contact"] = {
            "prospect_id": p["id"], "name": name, "first_name": (p["first_name"] or "").strip(),
            "title": p["title"] or "", "company": p["company"] or "", "company_id": p["company_id"],
            "location": p["location"] or "", "email_status": p["email_status"] or "",
        }
    return rows


async def annotate_sequence(state, rows: list[dict]) -> list[dict]:
    wanted = {(r.get("campaign_id"), r.get("prospect_id")) for r in rows
              if r.get("kind") == "sequence" and r.get("campaign_id")}
    threads: dict[tuple, list[dict]] = {}
    campaigns = sorted({c for c, _ in wanted})
    for chunk in _chunks(campaigns):
        for r in await _fetch(
                state,
                "SELECT id, campaign_id, prospect_id, step, status, send_at, sent_at FROM outbox "
                f"WHERE kind = 'sequence' AND campaign_id IN ({', '.join('?' for _ in chunk)}) "
                "ORDER BY step, created_at", chunk):
            key = (r["campaign_id"], r["prospect_id"])
            if key in wanted:
                threads.setdefault(key, []).append(
                    {k: r[k] for k in ("id", "step", "status", "send_at", "sent_at")})
    for row in rows:
        steps = threads.get((row.get("campaign_id"), row.get("prospect_id")))
        if row.get("kind") != "sequence" or not steps:
            row["sequence"] = None
            continue
        # A step written again after a rejection keeps one entry: the newest.
        latest: dict[int, dict] = {}
        for s in steps:
            latest[int(s["step"] or 1)] = s
        ordered = [latest[n] for n in sorted(latest)]
        row["sequence"] = {"total": max(len(ordered), int(row.get("step") or 1)), "steps": ordered}
    return rows


async def annotate_brief(state, config, rows: list[dict]) -> list[dict]:
    """``brief`` for each row; call after offers.annotate_outbox (offer_key)."""
    ids = sorted({r.get("generation_id") for r in rows if r.get("generation_id")})
    prompts: dict[str, str] = {}
    for chunk in _chunks(ids):
        for r in await _fetch(state, "SELECT id, prompt FROM email_generations "
                                     f"WHERE id IN ({', '.join('?' for _ in chunk)})", chunk):
            prompts[r["id"]] = r["prompt"] or ""
    for row in rows:
        step = int(row.get("step") or 1)
        brief = parse_brief(prompts.get(row.get("generation_id") or "", ""), step)
        configured = _configured_brief(config, row.get("offer_key") or "", step)
        if brief is not None and configured is not None and not brief["asks_for"]:
            brief["asks_for"] = configured["asks_for"]
        row["brief"] = brief or configured
    return rows


async def annotate_review(state, config, rows: list[dict]) -> list[dict]:
    """All three, for rows the desk shows."""
    await annotate_contacts(state, rows)
    await annotate_sequence(state, rows)
    await annotate_brief(state, config, rows)
    return rows
