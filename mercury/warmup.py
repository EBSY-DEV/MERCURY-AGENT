"""Inbox warm-up: health gates on top of the mailbox ramp, a week-by-week
plan for new inboxes, and DNS authentication checks.

What warm-up means here (and what it doesn't): Mercury does not run a
"warm-up network" that trades fake emails with other inboxes. It does the
parts that actually move reputation for a new mailbox — authenticate the
domain, start small, ramp slowly, stop when bounces climb.

Single source of truth: which inboxes exist, their daily caps and their ramp
come from ``channels.email.mailboxes`` in mercury.yaml (``warmup_start``,
``warmup_initial_cap``, ``warmup_weekly_increase``), through
``mercury.integrations.mailboxes.MailboxPool``. Without a mailboxes list the
pool is the one configured inbox, capped by ``max_daily_sends``, no ramp.

This module adds what the config can't know, kept in the ``warmup_inboxes``
table as an overlay keyed by mailbox address:

* **Health gate**, per mailbox, over the last 7 days (or since the last
  manual resume, if later). Sends are that mailbox's outreach sends; bounces
  are attributed through the outbox row they bounced (or the inbox the DSN
  arrived in).

    fewer than 20 sends          → ok ("not enough sends yet")
    bounce rate > 5%             → pause: the mailbox flips to ``paused`` and
                                   stays there until a human resumes it
    3% < bounce rate ≤ 5%        → hold: today's cap is yesterday's ramp cap
    otherwise                    → ok

* **Manual pause** from the dashboard (cap 0 until resumed).
* The **checklist** and free-form **notes**.

Enforcement: ``apply_health`` fills ``MailboxPool.gates`` and the pool's
``cap_on`` applies them, so the sender, the Outbox capacity card and this
page compute the same caps with the same rolling-24-hour counts. A paused
mailbox sends no cold email (openers or follow-ups are held, not cancelled);
replies to people who wrote back still go out, as with the ramp, bounded only
by ``max_daily_sends``. A gate can only ever lower a cap.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone

from mercury import bounces, metrics
from mercury.integrations.mailboxes import (
    MailboxPool,
    full_volume_on,
    local_today,
    mailbox_report,
)

logger = logging.getLogger("mercury.warmup")

MIN_SENDS_FOR_GATE = 20
HOLD_BOUNCE_RATE = 0.03
PAUSE_BOUNCE_RATE = 0.05
HEALTH_WINDOW_DAYS = 7
# The plan view lists at most this many days of a ramp.
MAX_PLAN_DAYS = 56

STATUSES = ("active", "paused")

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

CONFIG_HINT = """channels:
  email:
    provider: smtp
    warmup_initial_cap: 5        # day-1 cap of a new mailbox
    warmup_weekly_increase: 5    # added every 7 days, up to daily_cap
    mailboxes:
      - email: "you@yourcompany-mail.com"
        password_env: "MAILBOX_YOU_PASSWORD"   # the password goes in .env
        daily_cap: 30
        warmup_start: "2026-10-07"             # omit for an already-warm inbox"""


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


# ── Pure gate logic ───────────────────────────────────────────────────


def health_gate(sent: int, bounces: int) -> tuple[str, str]:
    """('ok'|'hold'|'pause', plain-language reason)."""
    if sent < MIN_SENDS_FOR_GATE:
        return "ok", f"not enough sends yet ({sent}/{MIN_SENDS_FOR_GATE} in the last 7 days)"
    rate = bounces / sent
    if rate > PAUSE_BOUNCE_RATE:
        return "pause", (
            f"bounce rate {rate:.1%} is over {PAUSE_BOUNCE_RATE:.0%} — sending from this "
            "inbox is paused. Clean the list (verified addresses only), then resume."
        )
    if rate > HOLD_BOUNCE_RATE:
        return "hold", (
            f"bounce rate {rate:.1%} is over {HOLD_BOUNCE_RATE:.0%} — volume is held at "
            "yesterday's level until it drops."
        )
    return "ok", f"bounce rate {rate:.1%} is healthy"


def ramp_cap(week: int, daily_cap: int, initial: int, weekly_increase: int) -> int:
    """The ramp's cap during plan week ``week`` (1-based), independent of
    the start date — the same formula as ``mailboxes.warmup_cap``."""
    return max(0, min(int(daily_cap), int(initial) + (max(1, week) - 1) * int(weekly_increase)))


def ramp_weeks(daily_cap: int, initial: int, weekly_increase: int) -> int:
    """Weeks spent below daily_cap (0 when the ramp starts at full volume)."""
    if daily_cap <= initial:
        return 0
    if weekly_increase <= 0:
        return 4  # never reaches the cap; the plan shows the first month
    return -(-(int(daily_cap) - int(initial)) // int(weekly_increase))


# ── The week-by-week plan ─────────────────────────────────────────────

WEEKS: list[dict] = [
    {"week": 0, "title": "Set up the inbox", "tasks": [
        ("dns", "SPF, DKIM and DMARC all pass for the sending domain (checked automatically)"),
        ("secondary_domain", "Send from a secondary domain (e.g. tryacme.com), never your main one — "
                             "if it gets burned, your real email keeps working"),
        ("domain_redirect", "Point the secondary domain's website at your real site so it doesn't look abandoned"),
        ("profile", "Real first and last name, a profile photo, and a short plain-text signature"),
        ("personal_emails", "Send 10-15 personal emails to people who will reply (colleagues, friends, clients)"),
        ("newsletters", "Subscribe to 2-3 newsletters you actually read, so the inbox gets normal mail"),
    ]},
    {"week": 1, "title": "Light sending", "tasks": [
        ("verified_only", "Only send to verified addresses — no catch-all ('risky') addresses yet"),
        ("reply_same_day", "Answer every reply the same day, even the no's"),
        ("seed_check", "Send one email to your own Gmail and Outlook addresses and confirm it lands in "
                       "the inbox, not spam or Promotions"),
        ("plain_text", "Keep emails plain text: no images, no tracking pixels, at most one link"),
    ]},
    {"week": 2, "title": "Build volume", "tasks": [
        ("bounce_check", "Keep the bounce rate under 3% (shown in Health above) — Mercury holds volume if it isn't"),
        ("keep_personal", "Keep sending a few normal, personal emails a day alongside the cold ones"),
        ("spam_folder", "Check this inbox's spam folder and move anything legitimate to the inbox"),
    ]},
    {"week": 3, "title": "Steady ramp", "tasks": [
        ("seed_check_2", "Repeat the seed check from week 1 — placement can change as volume grows"),
        ("reply_rate", "Look at reply rate: under 1% after ~100 sends means fix the list or the message, "
                       "not the volume"),
        ("opt_out", "Make it easy to say no — a plain 'just reply no and I won't follow up' line"),
    ]},
    {"week": 4, "title": "Reach target", "tasks": [
        ("postmaster", "Add the domain to Google Postmaster Tools and check the spam rate stays under 0.3%"),
        ("hold_target", "Stay at your target for two clean weeks before raising it"),
    ]},
    {"week": 5, "title": "After warm-up", "tasks": [
        ("second_inbox", "Need more volume? Warm up a second inbox instead of pushing this one past ~50/day"),
        ("weekly_review", "Check bounce and reply rates once a week; pause at the first sign of trouble"),
    ]},
]

TASK_KEYS = {key for w in WEEKS for key, _ in w["tasks"]}
AUTO_TASKS = {"dns"}


def _range(lo: int, hi: int) -> str:
    return f"{lo}/day" if lo == hi else f"{lo}-{hi}/day"


def week_range(week: int, daily_cap: int, initial: int, weekly_increase: int) -> str:
    """The volume a plan week stands for, phrased from the configured ramp.
    Week 4 ("Reach target") covers every remaining ramp week."""
    if week == 0:
        return "no cold email yet"
    if week >= 5:
        return f"{daily_cap}/day"
    if week < 4:
        c = ramp_cap(week, daily_cap, initial, weekly_increase)
        return f"{c}/day"
    last = max(4, ramp_weeks(daily_cap, initial, weekly_increase))
    return _range(ramp_cap(4, daily_cap, initial, weekly_increase),
                  ramp_cap(last, daily_cap, initial, weekly_increase))


def build_weeks(tasks_done: dict, dns_ok: bool, daily_cap: int,
                initial: int, weekly_increase: int) -> list[dict]:
    weeks = []
    for w in WEEKS:
        tasks = []
        for key, label in w["tasks"]:
            done = dns_ok if key == "dns" else bool(tasks_done.get(key))
            tasks.append({"key": key, "label": label, "done": done})
        weeks.append({
            "week": w["week"], "title": w["title"],
            "range": week_range(w["week"], daily_cap, initial, weekly_increase),
            "tasks": tasks,
        })
    return weeks


def dns_all_pass(result: dict | None) -> bool:
    """The auto 'dns' task: MX, SPF, DKIM and DMARC all pass."""
    if not result:
        return False
    statuses = {c["key"]: c["status"] for c in result.get("checks", [])}
    return all(statuses.get(k) == "pass" for k in ("mx", "spf", "dkim", "dmarc"))


# ── Overlay helpers ───────────────────────────────────────────────────


def _parse_ts(value) -> str | None:
    return str(value).replace(" ", "T") if value else None


def _load_tasks(raw) -> dict:
    try:
        parsed = json.loads(raw) if raw else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def dns_setting_key(domain: str) -> str:
    return f"warmup_dns:{domain.lower()}"


async def load_dns_result(state, domain: str) -> dict | None:
    raw = await state.get_setting(dns_setting_key(domain))
    try:
        return json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return None


def _owner(pool: MailboxPool, key: str):
    """The pool mailbox an outbox/event mailbox value belongs to, or None
    for a mailbox no longer in the config."""
    return pool.resolve(key)


async def _health_counts(state, pool: MailboxPool, since_by_email: dict[str, str]) -> dict[str, dict]:
    """``{email: {sent, bounces, replies}}`` for each pool mailbox over its
    own window. One query set per distinct window start."""
    out = {mb.email: {"sent": 0, "bounces": 0, "replies": 0} for mb in pool.mailboxes}
    by_since: dict[str, list[str]] = {}
    for email, since in since_by_email.items():
        by_since.setdefault(since, []).append(email)
    for since, emails in by_since.items():
        counts = await metrics.window_counts_by_mailbox(state.db_path, since)
        for key, c in counts.items():
            mb = _owner(pool, key)
            if mb is None or mb.email not in emails:
                continue
            for metric in ("sent", "bounces", "replies"):
                out[mb.email][metric] += c.get(metric, 0)
    return out


async def _bucket_counts(state, pool: MailboxPool, since_by_email: dict[str, str]) -> dict[str, dict]:
    """``{email: {BUCKET: n}}`` over each mailbox's own health window: what kind
    of bounces it got, as classified by ``mercury.bounces``."""
    out: dict[str, dict[str, int]] = {mb.email: {} for mb in pool.mailboxes}
    by_since: dict[str, list[str]] = {}
    for email, since in since_by_email.items():
        by_since.setdefault(since, []).append(email)
    for since, emails in by_since.items():
        for key, buckets in (await metrics.bucket_counts_by_mailbox(state.db_path, since)).items():
            mb = _owner(pool, key)
            if mb is None or mb.email not in emails:
                continue
            for bucket, n in buckets.items():
                out[mb.email][bucket] = out[mb.email].get(bucket, 0) + n
    return out


async def apply_health(state, pool: MailboxPool, *, persist: bool = True,
                       now: datetime | None = None) -> dict[str, dict]:
    """Evaluate every mailbox's health and set ``pool.gates``.

    Returns ``{email: {status, gate, reason, sent_7d, bounces, replies,
    since, row}}``. With ``persist``, a 'pause' verdict flips the overlay
    row to ``paused`` (it then needs a manual resume)."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    window_start = (now - timedelta(days=HEALTH_WINDOW_DAYS)).isoformat(timespec="seconds")
    rows = {r["email"]: r for r in await state.list_warmup_inboxes()}

    since_by_email = {}
    for mb in pool.mailboxes:
        since = window_start
        resumed = _parse_ts((rows.get(mb.email) or {}).get("resumed_at"))
        if resumed and resumed > since:
            since = resumed
        since_by_email[mb.email] = since
    counts = await _health_counts(state, pool, since_by_email)

    result: dict[str, dict] = {}
    gates: dict[str, str] = {}
    for mb in pool.mailboxes:
        row = rows.get(mb.email)
        c = counts.get(mb.email, {"sent": 0, "bounces": 0, "replies": 0})
        gate, reason = health_gate(c["sent"], c["bounces"])
        status = "paused" if (row and row.get("status") == "paused") else "active"
        if status == "paused":
            reason = (row.get("pause_reason") or "paused manually — resume when you're ready")
        elif gate == "pause":
            status = "paused"
            if persist and mb.email:
                await set_paused(state, mb.email, reason)
                row = await state.get_warmup_inbox(mb.email)
                logger.warning(f"Warm-up: {mb.email} paused automatically — {reason}")
        if status == "paused":
            gates[mb.email] = "paused"
        elif gate == "hold":
            gates[mb.email] = "hold"
        result[mb.email] = {
            "status": status, "gate": gate, "reason": reason,
            "sent_7d": c["sent"], "bounces": c["bounces"], "replies": c["replies"],
            "since": since_by_email[mb.email], "row": row or {},
        }
    pool.gates = gates
    # A 4.x.x throttling bounce halves that mailbox's cap for a week.
    try:
        factors = await bounces.load_throttles(
            state, [mb.email for mb in pool.mailboxes], now)
        for email in factors:
            if email in result:
                result[email]["throttled_until"] = await bounces.throttled_until(
                    state, email, now)
    except Exception as e:
        logger.warning(f"Warm-up: could not read mailbox throttles ({e}); "
                       "halving every mailbox's cap to be safe.")
        factors = {mb.email: bounces.THROTTLE_FACTOR for mb in pool.mailboxes}
    pool.cap_factors = factors
    return result


async def _ensure_row(state, email: str) -> dict:
    row = await state.get_warmup_inbox(email)
    if row is None:
        await state.add_warmup_inbox(email)
        row = await state.get_warmup_inbox(email)
    return row


async def set_paused(state, email: str, reason: str) -> None:
    await _ensure_row(state, email)
    await state.update_warmup_inbox(email, status="paused", paused_at=_utcnow_iso(),
                                    pause_reason=reason)


async def set_resumed(state, email: str) -> None:
    """Resume, and restart the health window so the old spike can't
    immediately re-pause a fixed inbox."""
    await _ensure_row(state, email)
    await state.update_warmup_inbox(email, status="active", paused_at=None,
                                    pause_reason="", resumed_at=_utcnow_iso())
    # A person looked at the mailbox: a BURNED verdict on its domain was theirs
    # to act on, so it no longer stands.
    if "@" in (email or ""):
        await bounces.clear_verdict(state, email.rsplit("@", 1)[-1])


async def set_task(state, email: str, key: str, done: bool) -> None:
    row = await _ensure_row(state, email)
    tasks = _load_tasks(row.get("tasks_json"))
    if done:
        tasks[key] = True
    else:
        tasks.pop(key, None)
    await state.update_warmup_inbox(email, tasks_json=json.dumps(tasks))


async def set_notes(state, email: str, notes: str) -> None:
    await _ensure_row(state, email)
    await state.update_warmup_inbox(email, notes=notes)


# ── The /api/warmup payload ───────────────────────────────────────────


def _plan(pool: MailboxPool, mb, today: date, full_on: date | None,
          sent_by_day: dict[str, int]) -> list[dict]:
    """Per-day ramp caps from warmup_start up to the day it reaches full
    volume (at most MAX_PLAN_DAYS), with what actually went out on past days."""
    if mb.warmup_start is None:
        return []
    start = mb.warmup_start
    span = (full_on - start).days + 1 if full_on else 28
    span = max(1, min(span, MAX_PLAN_DAYS))
    plan = []
    for i in range(span):
        d = start + timedelta(days=i)
        plan.append({
            "day": i + 1, "date": d.isoformat(),
            "cap": pool.base_cap_on(mb, d),
            "sent": None if d > today else sent_by_day.get(d.isoformat(), 0),
        })
    return plan


async def overview(state, config, pool: MailboxPool | None,
                   today: date | None = None) -> dict:
    """Everything the Warm-up tab shows, built from the same pool and caps
    the sender enforces (``mailbox_report``) plus the overlay."""
    today = today or local_today(config)
    if pool is None:
        report = mailbox_report(config, None, {}, today)
        return {**report, "active_email": None, "inboxes": [], "config_hint": CONFIG_HINT,
                "note": "Warm-up applies to the native providers (gmail, smtp); "
                        "Instantly runs its own."}

    health = await apply_health(state, pool)
    by_mailbox = await state.count_outbox_sent_today_by_mailbox()
    report = mailbox_report(config, pool, by_mailbox, today)
    rows_by_email = {r["email"]: r for r in report["mailboxes"] if r["stage"] != "removed"}

    # Daily volume per mailbox for the plan charts (UTC days).
    starts = [mb.warmup_start for mb in pool.mailboxes if mb.warmup_start]
    per_day: dict[str, dict[str, int]] = {}
    if starts:
        raw = await metrics.sent_by_day_by_mailbox(state.db_path, min(starts), today)
        for key, days in raw.items():
            owner = _owner(pool, key)
            if owner is None:
                continue
            bucket = per_day.setdefault(owner.email, {})
            for d, n in days.items():
                bucket[d] = bucket.get(d, 0) + n

    initial, inc = pool.warmup_initial_cap, pool.warmup_weekly_increase
    bucket_counts = await _bucket_counts(
        state, pool, {e: h["since"] for e, h in health.items() if "since" in h})
    verdicts = {}
    for mb in pool.mailboxes:
        if mb.domain and mb.domain not in verdicts:
            verdicts[mb.domain] = await bounces.get_verdict(state, mb.domain)

    inboxes = []
    for idx, mb in enumerate(pool.mailboxes):
        rep = rows_by_email.get(mb.email, {})
        h = health.get(mb.email, {})
        row = h.get("row") or {}
        full_on = full_volume_on(mb.daily_cap, mb.warmup_start, initial, inc)
        started = mb.warmup_start is not None and today >= mb.warmup_start
        day = (today - mb.warmup_start).days + 1 if started else None
        if mb.warmup_start is None:
            current_week = 5
        elif not started:
            current_week = 0
        elif full_on and today >= full_on:
            current_week = 5
        else:
            current_week = min(4, (day - 1) // 7 + 1)
        dns_ok = dns_all_pass(await load_dns_result(state, mb.domain)) if mb.domain else False
        sent_7d = h.get("sent_7d", 0)
        inboxes.append({
            "email": mb.email,
            "name": rep.get("name", ""),
            "domain": mb.domain,
            "primary": idx == 0,
            "is_sender": idx == 0,
            "legacy": mb.legacy,
            "status": h.get("status", "active"),
            "stage": rep.get("stage", ""),
            "accepts_new": mb.accepts_new,
            "configured": rep.get("configured"),
            "start_date": mb.warmup_start.isoformat() if mb.warmup_start else None,
            "full_on": full_on.isoformat() if full_on else None,
            "ramp_days": (full_on - mb.warmup_start).days if full_on else None,
            "day": day,
            "target_daily": mb.daily_cap,
            "today_cap": rep.get("cap_today", 0),
            "base_cap": rep.get("base_cap_today", 0),
            "sent_today": rep.get("sent_24h", 0),
            "remaining": rep.get("remaining", 0),
            "plan": _plan(pool, mb, today, full_on, per_day.get(mb.email, {})),
            "weeks": build_weeks(_load_tasks(row.get("tasks_json")), dns_ok,
                                 mb.daily_cap, initial, inc),
            "current_week": current_week,
            "health": {
                "sent_7d": sent_7d,
                "bounce_rate": round(h.get("bounces", 0) / sent_7d, 4) if sent_7d else None,
                "reply_rate": round(h.get("replies", 0) / sent_7d, 4) if sent_7d else None,
                "gate": h.get("gate", "ok"),
                "reason": h.get("reason", ""),
            },
            "paused_at": row.get("paused_at"),
            "pause_reason": row.get("pause_reason") or "",
            "notes": row.get("notes") or "",
            # Bounces by DSN bucket over the health window, and the 4.x.x
            # throttle and BURNED verdict that came from them.
            "bounce_buckets": {b: bucket_counts.get(mb.email, {}).get(b, 0)
                               for b in bounces.BUCKETS},
            "throttled_until": h.get("throttled_until"),
            "verdict": (verdicts.get(mb.domain) or {}).get("verdict"),
        })

    # Last stored DNS check per sending domain, so the Mailboxes table can show
    # every domain's records without running a live lookup for each one.
    domains: dict[str, dict | None] = {}
    for mb in pool.mailboxes:
        if mb.domain and mb.domain not in domains:
            stored = await load_dns_result(state, mb.domain)
            domains[mb.domain] = {
                "checks": [{"key": c.get("key"), "status": c.get("status")} for c in stored.get("checks", [])],
                "checked_at": stored.get("checked_at"),
                "all_pass": dns_all_pass(stored),
            } if stored else None

    removed = next((r for r in report["mailboxes"] if r["stage"] == "removed"), None)
    return {
        **{k: v for k, v in report.items() if k != "mailboxes"},
        "active_email": pool.primary.email or None,
        "single_inbox": pool.single_inbox,
        "warmup_initial_cap": initial,
        "warmup_weekly_increase": inc,
        "removed_sent_24h": removed["sent_24h"] if removed else 0,
        "inboxes": inboxes,
        "domains": domains,
        "config_hint": CONFIG_HINT,
    }


# ── DNS authentication checks ─────────────────────────────────────────

DNS_TIMEOUT_SECONDS = 2.0
DNS_CACHE_SECONDS = 600
DKIM_SELECTORS = ("google", "default", "selector1", "selector2", "k1", "s1", "mail", "dkim")

_dns_cache: dict[str, tuple[float, dict]] = {}


class DNSLookupError(Exception):
    """The lookup itself failed (timeout, no nameservers) — not 'no record'."""


def _default_resolve(name: str, rdtype: str) -> list[str]:
    """Resolve TXT/MX records. [] = definitively none; raises DNSLookupError
    when the answer is unknown. dnspython is imported lazily."""
    import dns.exception
    import dns.resolver

    resolver = dns.resolver.Resolver()
    resolver.timeout = DNS_TIMEOUT_SECONDS
    resolver.lifetime = DNS_TIMEOUT_SECONDS
    try:
        answer = resolver.resolve(name, rdtype)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []
    except (dns.exception.DNSException, OSError) as e:
        raise DNSLookupError(str(e) or type(e).__name__) from e
    if rdtype == "MX":
        return [f"{r.preference} {r.exchange.to_text().rstrip('.')}" for r in answer]
    return [b"".join(r.strings).decode("utf-8", "replace") for r in answer]


async def _lookup(resolve, name: str, rdtype: str):
    """(records, error) — error is a string when the lookup failed."""
    try:
        return await asyncio.to_thread(resolve, name, rdtype), None
    except Exception as e:  # noqa: BLE001 — any resolver failure is 'unknown'
        return [], str(e) or type(e).__name__


def _check(key: str, label: str, status: str, detail: str, record: str | None = None) -> dict:
    return {"key": key, "label": label, "status": status, "detail": detail, "record": record}


def _mx_check(records, error) -> dict:
    label = "Mail server (MX)"
    if error:
        return _check("mx", label, "unknown", f"Couldn't look up MX records ({error}). Try again shortly.")
    if not records:
        return _check("mx", label, "fail",
                      "No MX records — this domain can't receive mail, so replies and bounces "
                      "are lost. Add the MX records your email provider gives you.")
    return _check("mx", label, "pass",
                  "The domain can receive mail, so replies and bounce notices reach you.",
                  "; ".join(sorted(records)))


def _spf_check(records, error) -> dict:
    label = "SPF"
    if error:
        return _check("spf", label, "unknown", f"Couldn't look up SPF ({error}). Try again shortly.")
    spf = [r for r in records if r.lower().startswith("v=spf1")]
    if not spf:
        return _check("spf", label, "fail",
                      "No SPF record. Receivers can't tell which servers may send for this "
                      "domain. Add a TXT record like 'v=spf1 include:_spf.google.com ~all' "
                      "(Google) or 'v=spf1 include:spf.protection.outlook.com -all' (Microsoft).")
    record = spf[0]
    lower = record.lower()
    notes = []
    if "include:_spf.google.com" in lower:
        notes.append("Google Workspace is authorized.")
    if "include:spf.protection.outlook.com" in lower:
        notes.append("Microsoft 365 is authorized.")
    if len(spf) > 1:
        return _check("spf", label, "warn",
                      "There are multiple SPF records. Receivers treat that as an error and may "
                      "ignore SPF entirely — merge them into a single TXT record.",
                      " | ".join(spf))
    if re.search(r"(^|\s)\+all\b", lower):
        return _check("spf", label, "warn",
                      "SPF ends in '+all', which lets anyone send as this domain. Change it to "
                      "'~all' or '-all'.", record)
    if re.search(r"(^|\s)\?all\b", lower):
        return _check("spf", label, "warn",
                      "SPF ends in '?all' (neutral), which gives receivers no guidance. Change it "
                      "to '~all' or '-all'.", record)
    detail = "SPF is set up. " + " ".join(notes) if notes else (
        "SPF is set up. Make sure it includes the servers your email provider uses.")
    return _check("spf", label, "pass", detail.strip(), record)


def _dmarc_check(records, error) -> dict:
    label = "DMARC"
    if error:
        return _check("dmarc", label, "unknown", f"Couldn't look up DMARC ({error}). Try again shortly.")
    dmarc = [r for r in records if r.lower().replace(" ", "").startswith("v=dmarc1")]
    if not dmarc:
        return _check("dmarc", label, "fail",
                      "No DMARC record. Gmail and Yahoo require one for bulk senders. Add a TXT "
                      "record at _dmarc with 'v=DMARC1; p=none; rua=mailto:you@yourdomain' to "
                      "start, then tighten to p=quarantine once SPF and DKIM pass.")
    record = dmarc[0]
    m = re.search(r"(?:^|;)\s*p\s*=\s*(\w+)", record, re.IGNORECASE)
    policy = (m.group(1).lower() if m else "")
    if policy in ("quarantine", "reject"):
        return _check("dmarc", label, "pass",
                      f"DMARC is enforced (p={policy}), so spoofed mail is rejected or filtered.",
                      record)
    return _check("dmarc", label, "warn",
                  f"DMARC exists but only monitors (p={policy or 'missing'}). That's fine to "
                  "start; move to p=quarantine once SPF and DKIM pass for a couple of weeks.",
                  record)


def _dkim_check(results: list[tuple[str, list, str | None]]) -> dict:
    label = "DKIM"
    for selector, records, _error in results:
        for r in records:
            low = r.lower().replace(" ", "")
            if "v=dkim1" in low or "p=" in low:
                return _check("dkim", label, "pass",
                              f"Found a DKIM key at selector '{selector}', so your mail is "
                              "cryptographically signed.", f"{selector}: {r[:120]}")
    errored = any(err for _s, _r, err in results)
    detail = ("Couldn't find a DKIM key at common selectors — check your provider. "
              "In Google Workspace: Admin → Apps → Gmail → Authenticate email; in Microsoft 365: "
              "Defender → Email authentication → DKIM.")
    if errored:
        detail += " (Some lookups failed, so this may be a network problem.)"
    return _check("dkim", label, "unknown", detail)


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    d = value.strip().lower().rstrip(".")
    if "@" in d:
        d = d.rsplit("@", 1)[1]
    return d if DOMAIN_RE.match(d) else None


async def check_dns(domain: str, resolve=None, use_cache: bool = True) -> dict:
    """MX / SPF / DKIM / DMARC for a domain. All lookups run in parallel with
    a 2s timeout each; results are cached in memory for 10 minutes."""
    domain = domain.lower()
    now = time.monotonic()
    if use_cache and domain in _dns_cache:
        ts, cached = _dns_cache[domain]
        if now - ts < DNS_CACHE_SECONDS:
            return cached
    resolve = resolve or _default_resolve

    jobs = [
        _lookup(resolve, domain, "MX"),
        _lookup(resolve, domain, "TXT"),
        _lookup(resolve, f"_dmarc.{domain}", "TXT"),
        *[_lookup(resolve, f"{s}._domainkey.{domain}", "TXT") for s in DKIM_SELECTORS],
    ]
    mx, spf, dmarc, *dkim = await asyncio.gather(*jobs)
    checks = [
        _mx_check(*mx),
        _spf_check(*spf),
        _dkim_check([(s, recs, err) for s, (recs, err) in zip(DKIM_SELECTORS, dkim)]),
        _dmarc_check(*dmarc),
    ]
    result = {
        "domain": domain,
        "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "checks": checks,
    }
    _dns_cache[domain] = (now, result)
    return result


def clear_dns_cache():
    _dns_cache.clear()
