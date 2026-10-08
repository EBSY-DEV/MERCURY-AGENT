"""What stops mail from going out, kept apart by who may lift it.

Four things can stop cold mail. They are stored apart because each is lifted
differently, and a resume from one interface must never lift the others:

operator pause   settings ``operator_pause``. A person said stop (dashboard,
                 CLI, MCP). ``resume`` lifts this and nothing else.
health hold      settings ``sending_paused``, the bounce kill switch
                 (``mercury.bounces``). Resume leaves it in place. Only an
                 explicit clear (``mercury sending clear-hold``, or Clear hold
                 in the Outbox tab) lifts it, and that clear is the one place
                 the bounce counters restart.
mailbox holds    ``warmup_inboxes`` rows marked paused: the warm-up health gate,
                 a SENDER/BURNED bounce, or a person in the Mailboxes tab. Each
                 stops cold mail from one inbox and is lifted on that inbox.
compliance hold  ``compliance.postal_address`` is empty. The drain holds every
                 email until it is set.

Not holds: daily caps, the warm-up ramp, throttles, pacing, suppression,
stop-on-reply, the demo gate and the pre-send gate. They apply to each email
on every drain, whatever the switches above say.

In flight: an outbox row in ``sending`` was claimed before a pause or hold
landed. Its provider call is not interrupted (a send cut off mid-SMTP cannot
tell whether the message left, and retrying it risks a duplicate). It
finishes and is recorded like any other send. A row still ``sending`` after
30 minutes was interrupted (the process died); the next cycle puts it back to
``approved``, where any pause or hold keeps it.

Migration: before this split the operator and the bounce monitor both wrote
``sending_paused``. A value there that is one of the fixed strings an
operator switch used to write moves to ``operator_pause`` the first time it
is read. Anything else stays a health hold: it fails closed, and a person
clears it explicitly.
"""

from __future__ import annotations

from mercury.bounces import BOUNCE_COUNT_KEY, BUCKETS, KILL_SWITCH_KEY, load_counts

OPERATOR_KEY = "operator_pause"
HEALTH_KEY = KILL_SWITCH_KEY

# What the old single switch held when a person flipped it. Only these move;
# a free-text reason could have come from either side and stays a hold.
LEGACY_OPERATOR_REASONS = frozenset({"paused manually", "paused from dashboard", "paused from MCP"})

# The reason the Mailboxes tab writes for a pause a person asked for.
MANUAL_MAILBOX_REASON = "paused manually"

# How long a claimed row may sit in 'sending' before it counts as
# interrupted (mercury.state.StateManager.recover_stale_outbox).
STALE_SENDING_MINUTES = 30


async def migrate_legacy(state) -> None:
    """Move an operator pause out of the shared kill-switch key. Idempotent."""
    legacy = await state.get_setting(HEALTH_KEY)
    if legacy not in LEGACY_OPERATOR_REASONS:
        return
    async with state._connect() as db:
        # Re-read under the writer lock: another reader may already have
        # migrated the pause and the bounce monitor engaged a new hold.
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT value FROM settings WHERE key = ?", (HEALTH_KEY,)) as c:
            row = await c.fetchone()
        if not row or row[0] not in LEGACY_OPERATOR_REASONS:
            return
        await db.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "updated_at = CURRENT_TIMESTAMP WHERE COALESCE(settings.value, '') = ''",
            (OPERATOR_KEY, row[0]),
        )
        await db.execute("UPDATE settings SET value = '', updated_at = CURRENT_TIMESTAMP "
                         "WHERE key = ?", (HEALTH_KEY,))
        await db.commit()


async def operator_pause(state) -> str:
    await migrate_legacy(state)
    return await state.get_setting(OPERATOR_KEY)


async def health_hold(state) -> str:
    await migrate_legacy(state)
    return await state.get_setting(HEALTH_KEY)


async def blocking(state) -> tuple[str, str]:
    """``(kind, reason)`` when nothing may be claimed, else ``('', '')``.

    The sender asks this before staging and again before each claim, so a
    pause or hold that lands mid-drain stops the next email, not the next
    cycle. Kind is ``operator`` or ``health``; an operator pause is named
    first because it is the one the person at the keyboard can lift.
    """
    reason = await operator_pause(state)
    if reason:
        return "operator", reason
    reason = await state.get_setting(HEALTH_KEY)
    if reason:
        return "health", reason
    return "", ""


async def set_operator_pause(state, reason: str) -> None:
    await migrate_legacy(state)
    await state.set_setting(OPERATOR_KEY, reason)


async def clear_operator_pause(state) -> None:
    """Lift the operator pause. Health holds and bounce counters stay."""
    await migrate_legacy(state)
    await state.set_setting(OPERATOR_KEY, "")


async def bounce_counters(state) -> dict:
    try:
        total = int(await state.get_setting(BOUNCE_COUNT_KEY, "0") or 0)
    except ValueError:
        total = 0
    return {"bounces": total, "buckets": await load_counts(state)}


async def clear_health_hold(state) -> dict | None:
    """Lift the bounce kill switch and restart the count.

    Returns the counters as they stood before the reset, or None when there
    was no hold (the counters are then left alone: they feed the next check).
    """
    await migrate_legacy(state)
    async with state._connect() as db:
        # No sender may observe the hold cleared before its counter reset
        # succeeds. The snapshot and every write commit (or roll back) together.
        await db.execute("BEGIN IMMEDIATE")
        async with db.execute("SELECT key, value FROM settings") as c:
            settings = dict(await c.fetchall())
        reason = settings.get(HEALTH_KEY)
        if not reason:
            return None

        def count(key):
            try:
                return int(settings.get(key) or 0)
            except ValueError:
                return 0

        before = {"bounces": count(BOUNCE_COUNT_KEY),
                  "buckets": {bucket: count("bounce_bucket:" + bucket) for bucket in BUCKETS}}
        await db.execute("UPDATE settings SET value = '', updated_at = CURRENT_TIMESTAMP "
                         "WHERE key = ?", (HEALTH_KEY,))
        await db.executemany(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, '0', CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = '0', updated_at = CURRENT_TIMESTAMP",
            [(BOUNCE_COUNT_KEY,), *[("bounce_bucket:" + bucket,) for bucket in BUCKETS]],
        )
        await db.commit()
    return {"reason": reason, **before}


async def mailbox_holds(state) -> list[dict]:
    """One entry per paused inbox. Each stops cold mail from that inbox only."""
    out = []
    for row in await state.list_warmup_inboxes():
        if row.get("status") != "paused":
            continue
        reason = row.get("pause_reason") or MANUAL_MAILBOX_REASON
        out.append({
            "kind": "mailbox_paused",
            "scope": "mailbox",
            "mailbox": row.get("email") or "",
            "source": "operator" if reason == MANUAL_MAILBOX_REASON else "health",
            "reason": reason,
            "since": row.get("paused_at") or "",
        })
    return out


def compliance_hold(config) -> str:
    """Why the outbox drain holds everything for compliance, or ''. Only the
    native providers drain the outbox (legacy Instantly sends on its own)."""
    from mercury.integrations.mail_provider import NATIVE_PROVIDERS

    compliance = getattr(config, "compliance", None) if config is not None else None
    email = getattr(getattr(config, "channels", None), "email", None)
    if compliance is None or getattr(email, "provider", "") not in NATIVE_PROVIDERS:
        return ""
    if not (getattr(compliance, "postal_address", "") or "").strip():
        return ("compliance.postal_address is empty. Every commercial email needs a "
                "postal address, so the outbox holds until it is set")
    return ""


async def holds(state, config=None) -> list[dict]:
    """Every policy hold, global ones first. The operator pause is not here."""
    out = []
    health = await health_hold(state)
    if health:
        out.append({"kind": "bounce_kill_switch", "scope": "global", "source": "health",
                    "reason": health})
    compliance = compliance_hold(config)
    if compliance:
        out.append({"kind": "compliance", "scope": "global", "source": "config",
                    "reason": compliance})
    return out + await mailbox_holds(state)


async def in_flight(state, now: str | None = None) -> list[dict]:
    """Outbox rows already claimed: their send is under way or was cut off."""
    from datetime import datetime, timedelta, timezone

    now_dt = (datetime.fromisoformat(now) if now
              else datetime.now(timezone.utc).replace(tzinfo=None))
    stale_before = (now_dt - timedelta(minutes=STALE_SENDING_MINUTES)).isoformat()
    out = []
    for row in await state.get_outbox(status="sending", limit=50):
        claimed = str(row.get("updated_at") or "").replace(" ", "T")
        out.append({
            "id": row["id"],
            "to_email": row.get("to_email") or "",
            "mailbox": row.get("mailbox") or "",
            "step": row.get("step"),
            "kind": row.get("kind") or "",
            "claimed_at": claimed,
            # Sitting too long: the process died mid-send; the next cycle
            # re-queues it and any pause or hold then keeps it.
            "interrupted": bool(claimed) and claimed <= stale_before,
        })
    return out
