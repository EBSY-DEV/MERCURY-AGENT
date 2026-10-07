"""Inbox warm-up: an enforced send ramp, health gates, a week-by-week plan
for new inboxes, and DNS authentication checks.

What warm-up means here (and what it doesn't): Mercury does not run a
"warm-up network" that trades fake emails with other inboxes. It does the
parts that actually move reputation for a new mailbox — authenticate the
domain, start small, ramp slowly, stop when bounces climb — and enforces the
ramp in the sender so nobody can accidentally send 50 cold emails on day one.

Ramp: 28 days, day 1 = ``start_date``. Week 1 is 5,5,6,7,8,9,10/day, week 2
climbs 12→20, week 3 climbs 22→35, week 4 ramps to ``target_daily``. Every
value is clipped to ``target_daily`` and the curve never goes down. After
day 28 the inbox is ``complete`` and its cap is ``target_daily``.

Health gate (last 7 days, or since the last manual resume if that's later):
  * fewer than 20 sends            → ok ("not enough sends yet")
  * bounce rate > 5%               → pause: the inbox flips to ``paused`` and
                                     stays there until a human resumes it
  * 3% < bounce rate ≤ 5%          → hold: today's cap is yesterday's plan cap
  * otherwise                      → ok

Enforcement (native providers only — Instantly runs its own warm-up): the
sender's daily cap becomes ``min(channels.email.max_daily_sends, today_cap)``
while the sending inbox is ``warming``, and 0 while it is ``paused``.
``not_started`` / ``complete`` / no row leave the configured cap untouched.
Warm-up can only ever LOWER the cap.

Which inbox is "the sender": the outbox has no from-address column, so every
native send is attributed to one identity, resolved in this order:
``MERCURY_SENDER_EMAIL`` env var → ``persona.email`` from config (unless it
is the untrained template placeholder) → the first warming/paused inbox on
the plan. Only that inbox has real sent/bounce data; other inboxes on the
plan track their schedule and checklist, and their sent counts are 0.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

from mercury import metrics

logger = logging.getLogger("mercury.warmup")

RAMP_DAYS = 28
DEFAULT_TARGET = 30
MAX_RECOMMENDED_TARGET = 50
MIN_SENDS_FOR_GATE = 20
HOLD_BOUNCE_RATE = 0.03
PAUSE_BOUNCE_RATE = 0.05
HEALTH_WINDOW_DAYS = 7

STATUSES = ("not_started", "warming", "paused", "complete")

SENDER_ENV = "MERCURY_SENDER_EMAIL"
PLACEHOLDER_DOMAINS = {
    "yourcompany.com", "example.com", "example.org", "example.net", "company.com",
}

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9\-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).replace(tzinfo=None).isoformat(timespec="seconds")


# ── Pure plan / cap / gate logic ──────────────────────────────────────

_WEEK1 = [5, 5, 6, 7, 8, 9, 10]


def _linear(lo: float, hi: float, n: int = 7) -> list[int]:
    return [int(lo + (hi - lo) * i / (n - 1) + 0.5) for i in range(n)]


def default_target(config_max: int | None) -> int:
    """Warm-up target when the user hasn't set one: the configured daily
    cap, never above the 50/day a single inbox should do."""
    if not config_max or config_max <= 0:
        return DEFAULT_TARGET
    return min(int(config_max), MAX_RECOMMENDED_TARGET)


def ramp_plan(target_daily: int) -> list[int]:
    """The 28 daily caps. Clipped to target, monotonic non-decreasing."""
    target = max(1, int(target_daily))
    raw = _WEEK1 + _linear(12, 20) + _linear(22, 35)
    week4_from = raw[-1]
    raw += _linear(week4_from, max(target, week4_from), 8)[1:]
    caps, high = [], 0
    for value in raw:
        high = max(high, min(value, target))
        caps.append(high)
    return caps


def plan_day(start_date: date | None, today: date) -> int | None:
    """Raw 1-based plan day (may exceed 28, or be < 1 before the start)."""
    if start_date is None:
        return None
    return (today - start_date).days + 1


def plan_cap(day: int, target_daily: int) -> int:
    """Cap for a plan day, clamped into 1..28 (past the end → target)."""
    if day > RAMP_DAYS:
        return max(1, int(target_daily))
    return ramp_plan(target_daily)[max(1, day) - 1]


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


def effective_cap(status: str, day: int | None, target_daily: int, gate: str) -> int | None:
    """Today's enforced cap for an inbox, or None when warm-up doesn't apply.

    ``day`` is the raw plan day. Before the start date the day-1 cap applies
    (a scheduled warm-up is never looser than its first day)."""
    if status == "paused":
        return 0
    if status == "complete":
        return max(1, int(target_daily))
    if status != "warming":
        return None
    d = 1 if day is None or day < 1 else day
    if gate == "pause":
        return 0
    if gate == "hold":
        d = max(1, d - 1)
    return plan_cap(d, target_daily)


def week_for_day(day: int | None) -> int:
    """0 = setup (not started), 1-4 = ramp weeks, 5 = after warm-up."""
    if day is None or day < 1:
        return 0
    if day > RAMP_DAYS:
        return 5
    return (day - 1) // 7 + 1


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


def _week_range(week: int, target: int) -> str:
    if week == 0:
        return "no cold email yet"
    if week == 5:
        return f"{target}/day"
    caps = ramp_plan(target)[(week - 1) * 7: week * 7]
    lo, hi = min(caps), max(caps)
    return f"{lo}/day" if lo == hi else f"{lo}-{hi}/day"


def build_weeks(tasks_done: dict, dns_ok: bool, target: int) -> list[dict]:
    weeks = []
    for w in WEEKS:
        tasks = []
        for key, label in w["tasks"]:
            done = dns_ok if key == "dns" else bool(tasks_done.get(key))
            tasks.append({"key": key, "label": label, "done": done})
        weeks.append({
            "week": w["week"], "title": w["title"],
            "range": _week_range(w["week"], target), "tasks": tasks,
        })
    return weeks


def dns_all_pass(result: dict | None) -> bool:
    """The auto 'dns' task: MX, SPF, DKIM and DMARC all pass."""
    if not result:
        return False
    statuses = {c["key"]: c["status"] for c in result.get("checks", [])}
    return all(statuses.get(k) == "pass" for k in ("mx", "spf", "dkim", "dmarc"))


# ── Identity resolution ───────────────────────────────────────────────

def is_valid_email(email: str | None) -> bool:
    return bool(email) and bool(EMAIL_RE.match(email.strip()))


def is_placeholder_email(email: str | None) -> bool:
    if not is_valid_email(email):
        return True
    return email.strip().lower().rsplit("@", 1)[1] in PLACEHOLDER_DOMAINS


def resolve_sender_email(config_email: str | None, inboxes: list[dict]) -> str | None:
    """The inbox every native send is attributed to (see module docstring)."""
    override = (os.environ.get(SENDER_ENV) or "").strip().lower()
    if is_valid_email(override):
        return override
    if not is_placeholder_email(config_email):
        return config_email.strip().lower()
    for row in inboxes:
        if row.get("status") in ("warming", "paused") and row.get("start_date"):
            return row["email"]
    return None


# ── Async helpers over StateManager ───────────────────────────────────

def _parse_date(value) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


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


async def inbox_view(
    state,
    row: dict,
    *,
    is_sender: bool,
    config_max: int | None,
    today: date | None = None,
    persist: bool = True,
) -> dict:
    """Evaluate one inbox into the API shape.

    With ``persist`` (the default), state transitions the evaluation finds
    are written back: past day 28 → ``complete``; a 'pause' health gate on a
    warming inbox → ``paused`` (which then needs a manual resume)."""
    today = today or utc_today()
    email = row["email"]
    status = row.get("status") or "not_started"
    start = _parse_date(row.get("start_date"))
    target = int(row.get("target_daily") or default_target(config_max))
    raw_day = plan_day(start, today) if status != "not_started" else None

    if status == "warming" and raw_day is not None and raw_day > RAMP_DAYS:
        status = "complete"
        if persist and row.get("_persisted", True):
            await state.update_warmup_inbox(email, status="complete")

    # Health — only the sending identity has real send data.
    if is_sender:
        since_dt = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=HEALTH_WINDOW_DAYS)
        resumed = row.get("resumed_at")
        since = since_dt.isoformat(timespec="seconds")
        if resumed and str(resumed).replace(" ", "T") > since:
            since = str(resumed)
        counts = await metrics.window_counts(state.db_path, since)
    else:
        counts = dict.fromkeys(metrics.METRICS, 0)
    sent_7d = counts["sent"]
    gate, reason = health_gate(sent_7d, counts["bounces"])
    if not is_sender:
        reason = "no send data — Mercury only sends from the active inbox"

    if status == "warming" and gate == "pause":
        status = "paused"
        if persist and row.get("_persisted", True):
            await state.update_warmup_inbox(
                email, status="paused", paused_at=_utcnow_iso(),
                pause_reason=reason,
            )
    elif status == "paused":
        reason = row.get("pause_reason") or "paused manually — resume when you're ready"

    today_cap = effective_cap(status, raw_day, target, gate)

    # Daily plan with actual volume per day.
    plan = []
    sent_today = 0
    if is_sender:
        sent_today = (await metrics.sent_by_day(state.db_path, today, today)).get(today.isoformat(), 0)
    if start is not None:
        end = start + timedelta(days=RAMP_DAYS - 1)
        by_day = (await metrics.sent_by_day(state.db_path, start, min(end, today))
                  if is_sender else {})
        for i, cap in enumerate(ramp_plan(target)):
            d = start + timedelta(days=i)
            plan.append({
                "day": i + 1, "date": d.isoformat(), "cap": cap,
                "sent": None if d > today else by_day.get(d.isoformat(), 0),
            })

    domain = email.rsplit("@", 1)[-1]
    dns_ok = dns_all_pass(await load_dns_result(state, domain))
    day_out = None
    if raw_day is not None and raw_day >= 1:
        day_out = min(raw_day, RAMP_DAYS)

    return {
        "email": email,
        "is_sender": is_sender,
        "status": status,
        "start_date": start.isoformat() if start else None,
        "day": day_out,
        "target_daily": target,
        "today_cap": today_cap,
        "sent_today": sent_today,
        "plan": plan,
        "weeks": build_weeks(_load_tasks(row.get("tasks_json")), dns_ok, target),
        "current_week": (5 if status == "complete" else week_for_day(raw_day))
                        if status != "not_started" else 0,
        "health": {
            "sent_7d": sent_7d,
            "bounce_rate": round(counts["bounces"] / sent_7d, 4) if sent_7d else None,
            "reply_rate": round(counts["replies"] / sent_7d, 4) if sent_7d else None,
            "gate": gate,
            "reason": reason,
        },
        "notes": row.get("notes") or "",
    }


def virtual_row(email: str) -> dict:
    """The active sender with no warm-up row yet (shown, never persisted)."""
    return {"email": email.lower(), "status": "not_started", "start_date": None,
            "target_daily": None, "notes": "", "tasks_json": "{}", "_persisted": False}


async def overview(state, config_email: str | None, config_max: int | None,
                   today: date | None = None) -> dict:
    """The ``/api/warmup`` payload."""
    rows = await state.list_warmup_inboxes()
    active = resolve_sender_email(config_email, rows)
    if active and not any(r["email"] == active for r in rows):
        rows = [virtual_row(active)] + rows
    inboxes = []
    for row in rows:
        inboxes.append(await inbox_view(
            state, row, is_sender=(row["email"] == active),
            config_max=config_max, today=today,
        ))
    # Active sender first, then the rest in plan order.
    inboxes.sort(key=lambda i: not i["is_sender"])
    return {"active_email": active, "inboxes": inboxes}


async def sender_daily_cap(state, config) -> int | None:
    """The warm-up cap for the sending inbox today, or None if warm-up
    doesn't constrain it. Callers must take ``min()`` with the configured
    cap — this never raises it."""
    rows = await state.list_warmup_inboxes()
    email_cfg = getattr(getattr(config, "persona", None), "email", None)
    active = resolve_sender_email(email_cfg, rows)
    if not active:
        return None
    row = next((r for r in rows if r["email"] == active), None)
    if row is None:
        return None
    config_max = getattr(config.channels.email, "max_daily_sends", None)
    view = await inbox_view(state, row, is_sender=True, config_max=config_max)
    if view["status"] == "paused":
        return 0
    if view["status"] == "warming":
        return view["today_cap"]
    return None


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
