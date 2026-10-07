"""The demo gate: hold an offer's emails until its per-prospect demo exists.

Some offers promise something already built for that one business ("a line
that answers as Al-Air", "a draft homepage with your photos"). Sent before
the demo exists, that email makes a false claim. So every sequence email of
an offer with ``requires_demo: true`` waits until the prospect's demo is
marked ``ready``.

The offer comes from the outbox row's ``offer_key`` (else its campaign's).
The offer router (#57) stamps that key at write time; until it does, rows
carry no offer and nothing here applies. Building the demo itself is out of
scope: this module is the bookkeeping and the gate.

The gate fails closed. A key that is not in ``offers:``, a row with no
prospect, or any error while checking holds the email instead of sending it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("mercury.demos")

DEMO_STATUSES = ("requested", "ready", "retired")
DEFAULT_RETIRE_AFTER_DAYS = 14


@dataclass(frozen=True)
class DemoVerdict:
    """Whether one email may leave as far as the demo gate is concerned."""
    held: bool
    # Stable code: no_demo | demo_requested | unknown_offer | no_prospect |
    # no_config | error. Empty when the email may go.
    code: str = ""
    # One plain sentence for the Outbox, the CLI and the log.
    reason: str = ""
    offer_key: str = ""
    requires_demo: bool = False

    def __bool__(self):  # truthy = may send, like GateResult
        return not self.held


PASS = DemoVerdict(held=False)


def offers_by_key(config) -> dict:
    """offers[] from mercury.yaml by key. A config without offers is {}."""
    return {o.key: o for o in (getattr(config, "offers", None) or [])}


def retire_after_days(config) -> int:
    demos = getattr(config, "demos", None)
    return int(getattr(demos, "retire_after_days", DEFAULT_RETIRE_AFTER_DAYS))


def verdict(offer_key: str, offers: dict | None, demo_status: str | None,
            prospect_id: str = "") -> DemoVerdict:
    """The gate's decision, from facts already looked up. ``offers`` None
    means the offers could not be read, which holds any row with an offer."""
    offer_key = (offer_key or "").strip().lower()
    if not offer_key:
        return PASS
    if offers is None:
        return DemoVerdict(True, "no_config",
                           "Mercury could not read the offers in mercury.yaml, so it holds "
                           "this email until it can tell whether it needs a demo.", offer_key)
    offer = offers.get(offer_key)
    if offer is None:
        return DemoVerdict(True, "unknown_offer",
                           f"The {offer_key} offer is not in mercury.yaml, so Mercury can't "
                           "tell whether it needs a demo. Add it under offers.", offer_key)
    if not offer.requires_demo:
        return DemoVerdict(False, offer_key=offer_key)
    if not prospect_id:
        return DemoVerdict(True, "no_prospect",
                           "This email has no contact, so there is no demo it could point to.",
                           offer_key, True)
    if demo_status == "ready":
        return DemoVerdict(False, offer_key=offer_key, requires_demo=True)
    if demo_status == "requested":
        return DemoVerdict(True, "demo_requested",
                           "The demo is requested but not marked ready yet.",
                           offer_key, True)
    return DemoVerdict(True, "no_demo",
                       "No demo is registered for this contact yet.",
                       offer_key, True)


async def check_outbox_item(state, config, item: dict) -> DemoVerdict:
    """The Sender's check for one due outbox row. Replies are never gated:
    they answer someone who wrote to us. A missing demo is registered as
    'requested' so it shows up in `mercury demos` as work to do."""
    try:
        if item.get("kind") != "sequence":
            return PASS
        offer_key = await state.outbox_offer_key(item)
        if not offer_key:
            return PASS
        offers = offers_by_key(config)
        offer = offers.get(offer_key)
        prospect_id = item.get("prospect_id") or ""
        demo = None
        if offer is not None and offer.requires_demo and prospect_id:
            demo = await state.find_live_demo(prospect_id, offer_key)
        result = verdict(offer_key, offers, demo["status"] if demo else None, prospect_id)
        if result.code == "no_demo":
            await state.request_demo(prospect_id, offer_key, offer.demo_kind)
        return result
    except Exception as exc:  # fail closed: an unreadable answer holds the email
        logger.warning(f"Demo gate: could not check outbox row {item.get('id')}: {exc}")
        return DemoVerdict(True, "error",
                           f"Mercury could not check this email's demo ({type(exc).__name__}), "
                           "so it is holding it.", (item.get("offer_key") or ""), True)


async def check_campaign(state, config, campaign) -> DemoVerdict:
    """Instantly deploys a whole sequence at once, so a campaign whose offer
    needs demos waits until every contact in it has a ready one."""
    try:
        offer_key = (getattr(campaign, "offer_key", "") or "").strip().lower()
        if not offer_key:
            return PASS
        offers = offers_by_key(config)
        offer = offers.get(offer_key)
        if offer is None or not offer.requires_demo:
            return verdict(offer_key, offers, None)
        waiting = 0
        for prospect_id in campaign.prospect_ids:
            demo = await state.find_live_demo(prospect_id, offer_key)
            if demo is None:
                await state.request_demo(prospect_id, offer_key, offer.demo_kind)
            if not demo or demo["status"] != "ready":
                waiting += 1
        if waiting:
            return DemoVerdict(True, "no_demo",
                               f"{waiting} of {len(campaign.prospect_ids)} contacts are waiting "
                               "for their demo.", offer_key, True)
        return DemoVerdict(False, offer_key=offer_key, requires_demo=True)
    except Exception as exc:
        logger.warning(f"Demo gate: could not check campaign {getattr(campaign, 'id', '')}: {exc}")
        return DemoVerdict(True, "error",
                           f"Mercury could not check this campaign's demos ({type(exc).__name__}).",
                           requires_demo=True)


async def register_requests(state, config, prospect_ids, offer_key: str) -> int:
    """Register 'requested' demos for prospects of a demo offer, so whoever
    builds demos sees the work the moment the emails are queued."""
    offer = offers_by_key(config).get((offer_key or "").strip().lower())
    if offer is None or not offer.requires_demo:
        return 0
    created = 0
    for prospect_id in prospect_ids:
        _demo_id, new = await state.request_demo(prospect_id, offer.key, offer.demo_kind)
        created += int(new)
    return created


def _row_demo(row: dict, offers: dict | None) -> dict:
    v = verdict(row.get("offer") or "", offers, row.get("demo_status"), row.get("prospect_id") or "")
    return {
        "held": v.held, "code": v.code, "reason": v.reason,
        "offer_key": row.get("offer") or "", "requires_demo": v.requires_demo,
        "demo_id": row.get("demo_id") or "", "status": row.get("demo_status") or "",
    }


async def annotate_outbox(state, config, rows: list[dict]) -> list[dict]:
    """Add ``demo`` to each row: None when no offer applies, else what the
    gate decides and why. ``config`` None means it could not be loaded."""
    ids = [r["id"] for r in rows if r.get("kind") == "sequence"
           and r.get("status") in ("pending_review", "approved")]
    context = {r["id"]: r for r in await state.queued_offer_outbox(ids)} if ids else {}
    offers = offers_by_key(config) if config is not None else None
    for row in rows:
        ctx = context.get(row.get("id"))
        row["demo"] = _row_demo(ctx, offers) if ctx else None
    return rows


async def waiting_for_demo(state, config) -> list[dict]:
    """One entry per (contact, offer) whose queued emails the gate holds,
    with the earliest held step. ``config`` None holds every offer row."""
    offers = offers_by_key(config) if config is not None else None
    seen: dict[tuple[str, str], dict] = {}
    for row in await state.queued_offer_outbox():
        demo = _row_demo(row, offers)
        if not demo["held"]:
            continue
        key = (row.get("prospect_id") or row["id"], demo["offer_key"])
        if key in seen and int(seen[key]["step"]) <= int(row.get("step") or 1):
            continue
        name = " ".join(p for p in (row.get("first_name"), row.get("last_name")) if p)
        seen[key] = {
            "outbox_id": row["id"], "prospect_id": row.get("prospect_id") or "",
            "to_email": row.get("to_email") or "", "name": name,
            "company": row.get("company") or "", "step": int(row.get("step") or 1),
            "status": row.get("status"), "send_at": row.get("send_at"),
            "subject": row.get("subject") or "",
            "offer_key": demo["offer_key"],
            "kind": getattr(offers.get(demo["offer_key"]), "demo_kind", "") if offers else "",
            "demo_id": demo["demo_id"], "demo_status": demo["status"],
            "code": demo["code"], "reason": demo["reason"],
        }
    return list(seen.values())


async def retire_stale_demos(state, config) -> int:
    """Retire ready demos nobody answered: ``demos.retire_after_days`` after
    the last email to a contact who never replied, once nothing is queued."""
    days = retire_after_days(config)
    if days <= 0:
        return 0
    retired = 0
    for demo in await state.demos_due_for_retirement(days):
        if await state.retire_demo(demo["id"], f"no reply {days} days after the last email"):
            retired += 1
    if retired:
        logger.info(f"Demos: retired {retired} demo(s) with no reply after {days} days.")
        await state.log_action(action_type="demos_retired", agent="main",
                               details={"retired": retired, "after_days": days})
    return retired
