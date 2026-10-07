"""Deliverability health: one verdict per sending domain (the 1% rule).

Zero replies says nothing on its own. It could be the copy, the list, or
mail landing in spam, and on a small sample it is not even a signal yet:
practitioner data puts the line at about 150 sends before "0 replies"
means anything, and a 10/10 mail-tester score does not predict where Gmail
puts a message. This module answers the narrower question it can answer
honestly from Mercury's own data: *is there enough evidence about this
domain to keep it or to cancel it?*

Per sending domain (every mailbox on the domain added together) it reports
outreach sends, bounces and replies over the last 7 and 14 days, the
domain's sending age, and a verdict over the last ``VERDICT_WINDOW_DAYS``.
The ladder, checked in this order:

    ===================  ===================================================
    CANCEL_CANDIDATE     bounce composition says the domain is burned
                         (a 5.7.6xx reputation block). Needs classified
                         bounces, see ``bounce_composition``.
    TOO_YOUNG            sending age under 30 days, or nothing sent yet
    KEEP                 200+ sends and replies at or above 1%
    CANCEL_CANDIDATE     0 replies on 150+ sends, or under 1% after 200
    INSUFFICIENT_DATA    everything else: under 200 sends for the reply
                         rate (under 50 for the bounce rate)
    ===================  ===================================================

Never "healthy" without evidence: a domain is only KEEP once 200 sends have
produced at least 1% replies. A cancel candidate is a candidate. The
placement test (``mercury mail placement``, see mercury/placement.py) is what
tells a burned domain apart from copy that lands in spam from anywhere.

Definitions are the ones ``mercury.metrics`` uses everywhere else: a send is
an outbox row with ``status = 'sent'`` and ``kind = 'sequence'`` (Mercury's
own replies are not outreach); a reply is an inbound human reply, one per
prospect per day, out-of-office excluded; a bounce is the handler's
``bounce`` event, attributed to the mailbox that sent the bounced email.
Rows recorded before mailbox tracking (mailbox '') belong to the legacy
mailbox, as everywhere else. Events no send can be traced to are counted
as unattributed and charged to no domain.

**Sending age** is days since the domain's earliest evidence of sending:
its first outreach send in the outbox, or the earliest ``warmup_start`` of
its configured mailboxes, whichever is older. It is not the registration
date. A domain warmed elsewhere reads young until Mercury has 30 days of
its history, which errs on the side of not judging it.

A high bounce rate does not change the verdict while bounces are not
classified: unclassified, a bounce could be a bad address (a list problem)
as easily as a blocked sender (a domain problem). It shows as a flag, and
the per-mailbox warm-up gate (mercury/warmup.py) already pauses an inbox
past 5%.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

import aiosqlite

from mercury import metrics

# ── The ladder's thresholds ──────────────────────────────────────────

MIN_AGE_DAYS = 30            # younger than this: TOO_YOUNG
MIN_SENDS_BOUNCE = 50        # the bounce rate means something from here
MIN_SENDS_REPLY = 200        # the reply rate means something from here
ZERO_REPLY_SENDS = 150       # 0 replies on this many sends is a signal
KEEP_REPLY_RATE = 0.01       # the 1% rule
VERDICT_WINDOW_DAYS = 30     # the verdict reads this many days of history
SHORT_WINDOWS = (7, 14)      # shown next to it

DEFAULT_MAX_BOUNCE_RATE = 0.05

# verdict -> (label, tone). Tones are the dashboard's status tones.
VERDICTS = {
    "CANCEL_CANDIDATE": ("Cancel candidate", "bad"),
    "INSUFFICIENT_DATA": ("Not enough data", "waiting"),
    "TOO_YOUNG": ("Too young", "idle"),
    "KEEP": ("Keep", "good"),
}
# Worst first, for sorting.
SEVERITY = ("CANCEL_CANDIDATE", "INSUFFICIENT_DATA", "TOO_YOUNG", "KEEP")

THRESHOLDS = {
    "min_age_days": MIN_AGE_DAYS,
    "min_sends_bounce": MIN_SENDS_BOUNCE,
    "min_sends_reply": MIN_SENDS_REPLY,
    "zero_reply_sends": ZERO_REPLY_SENDS,
    "keep_reply_rate": KEEP_REPLY_RATE,
    "window_days": VERDICT_WINDOW_DAYS,
}


# ── Bounce composition: the seam for classified bounces ───────────────


@dataclass
class BounceComposition:
    """The bounces of one domain over the verdict window.

    ``buckets`` is empty while bounces carry no DSN classification. Once
    they do, it maps a bucket (LIST, SENDER, BURNED, THROTTLE, NOISE) to a
    count and ``classified`` is True.
    """
    total: int = 0
    classified: bool = False
    buckets: dict[str, int] = field(default_factory=dict)

    @property
    def burned(self) -> bool:
        """A receiver blocked the domain for its reputation (5.7.6xx)."""
        return self.classified and self.buckets.get("BURNED", 0) > 0

    @property
    def counted(self) -> int:
        """Bounces that count toward the bounce rate (NOISE never does)."""
        if not self.classified:
            return self.total
        return max(0, self.total - self.buckets.get("NOISE", 0))


async def bounce_composition(db_path: str, mailboxes: set[str], since: str,
                             total: int) -> BounceComposition:
    """How a domain's bounces since ``since`` break down by cause.

    ``mailboxes`` are the raw ``outbox.mailbox`` values that belong to the
    domain ('' when the domain owns the legacy rows) and ``total`` is the
    de-duplicated bounce count already attributed to them.

    This is the one place bounce classification plugs in. Bounces carry no
    SMTP status code yet, so today it returns the total, unclassified, and
    the verdict never calls a domain burned from bounces alone. When bounce
    records gain a DSN bucket, read them here (attributing each bounce to a
    mailbox with ``metrics._event_mailbox_sql()``, as the totals are) and
    return ``classified=True`` with the counts per bucket. Nothing else in
    the ladder has to change.
    """
    return BounceComposition(total=total)


# ── The verdict ladder (pure) ────────────────────────────────────────


def _pct(rate: float) -> str:
    return f"{rate * 100:.1f}%"


def _plural(n: int, word: str, plural: str | None = None) -> str:
    return f"{n} {word if n == 1 else (plural or word + 's')}"


def verdict(*, age_days: int | None, sent: int, replies: int,
            bounces: BounceComposition | None = None, age_source: str | None = None,
            max_bounce_rate: float = DEFAULT_MAX_BOUNCE_RATE,
            window_days: int = VERDICT_WINDOW_DAYS) -> dict:
    """One domain's verdict from its numbers over the verdict window.

    Returns ``{verdict, label, tone, reason, next, flags, reply_rate,
    bounce_rate}``. A rate is None until its sample is big enough to mean
    something (200 sends for replies, 50 for bounces)."""
    bounces = bounces or BounceComposition()
    window = f"in the last {window_days} days"
    reply_rate = replies / sent if sent >= MIN_SENDS_REPLY else None
    bounce_rate = bounces.counted / sent if sent >= MIN_SENDS_BOUNCE else None

    flags: list[dict] = []
    if bounce_rate is not None and max_bounce_rate and bounce_rate > max_bounce_rate:
        flags.append({"tone": "bad", "text": (
            f"Bounce rate {_pct(bounce_rate)} is over the {_pct(max_bounce_rate)} limit. "
            "Clean the list: verified addresses only.")})

    def out(key: str, reason: str, nxt: str) -> dict:
        label, tone = VERDICTS[key]
        return {"verdict": key, "label": label, "tone": tone, "reason": reason,
                "next": nxt, "flags": flags,
                "reply_rate": round(reply_rate, 4) if reply_rate is not None else None,
                "bounce_rate": round(bounce_rate, 4) if bounce_rate is not None else None}

    placement_hint = ("Run a placement test (mercury mail placement) before cancelling: "
                      "it tells a burned domain apart from copy that lands in spam anywhere.")

    if bounces.burned:
        n = bounces.buckets.get("BURNED", 0)
        return out("CANCEL_CANDIDATE",
                   f"Receivers blocked this domain for its reputation "
                   f"({_plural(n, 'bounce')} with a 5.7.6xx code).",
                   "Stop sending from it and move its inboxes to a fresh domain.")

    if age_days is None:
        return out("TOO_YOUNG", "It hasn't sent any outreach yet.",
                   "Nothing to judge until it starts sending.")
    if age_days < MIN_AGE_DAYS:
        left = MIN_AGE_DAYS - age_days
        what = "Its warm-up started" if age_source == "warmup_start" else "It started sending"
        when = "today" if age_days == 0 else f"{_plural(age_days, 'day')} ago"
        return out("TOO_YOUNG",
                   f"{what} {when}. A domain needs {MIN_AGE_DAYS} days before its "
                   "numbers mean anything.",
                   f"Keep the ramp going and check again in {_plural(left, 'day')}.")

    sends = _plural(sent, "send")
    reps = _plural(replies, "reply", "replies")
    if sent >= MIN_SENDS_REPLY and replies / sent >= KEEP_REPLY_RATE:
        return out("KEEP",
                   f"{reps} on {sends} ({_pct(replies / sent)}) {window}, "
                   f"at or above the 1% line.",
                   "Keep this domain. Its mail is reaching people who answer.")
    if sent >= ZERO_REPLY_SENDS and replies == 0:
        return out("CANCEL_CANDIDATE",
                   f"0 replies on {sends} {window}. That many sends without a single "
                   "reply usually means the mail is landing in spam.",
                   placement_hint)
    if sent >= MIN_SENDS_REPLY:
        return out("CANCEL_CANDIDATE",
                   f"{reps} on {sends} ({_pct(replies / sent)}) {window}, "
                   f"under the 1% line after {MIN_SENDS_REPLY} sends.",
                   placement_hint)

    more = MIN_SENDS_REPLY - sent
    if sent < MIN_SENDS_BOUNCE:
        reason = (f"Only {sends} {window}. The bounce rate needs {MIN_SENDS_BOUNCE} sends "
                  f"and the reply rate needs {MIN_SENDS_REPLY}.")
    elif replies == 0:
        reason = (f"0 replies on {sends} {window}. Below {ZERO_REPLY_SENDS} sends that is "
                  "not a signal yet.")
    else:
        reason = (f"{reps} on {sends} {window}. The reply rate needs "
                  f"{MIN_SENDS_REPLY} sends before it means anything.")
    return out("INSUFFICIENT_DATA", reason,
               f"Keep sending. About {_plural(more, 'more send')} for a reply-rate verdict.")


# ── Per-domain aggregation ───────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _domain(email: str) -> str:
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[1] if "@" in email else ""


def _parse_day(value) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace(" ", "T")[:19]).date()
    except ValueError:
        return None


async def _first_sends(db_path: str) -> dict[str, date]:
    """``{raw mailbox value: day of its first outreach send}``."""
    out: dict[str, date] = {}
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COALESCE(mailbox, ''), MIN(datetime(sent_at)) FROM outbox "
            "WHERE status = 'sent' AND kind = 'sequence' AND sent_at IS NOT NULL "
            "GROUP BY 1"
        ) as cursor:
            for mailbox, first in await cursor.fetchall():
                day = _parse_day(first)
                if day:
                    out[mailbox or ""] = day
    return out


def _resolver(pool, fallback_email: str):
    """raw outbox/event mailbox value -> owning mailbox address ('' unknown)."""
    legacy = ""
    if pool is not None:
        legacy = (pool.legacy.email or "").lower()
    legacy = legacy or (fallback_email or "").strip().lower()

    def resolve(key: str) -> str:
        key = (key or "").strip().lower()
        if key == metrics.UNATTRIBUTED:
            return ""
        if not key:
            return legacy
        if pool is not None and pool.single_inbox:
            # One inbox: whatever a row recorded, it went out through it.
            return legacy or key
        return key
    return resolve


async def domain_report(state, config, pool, *, now: datetime | None = None) -> dict:
    """The ``mercury health`` / ``/api/health`` payload."""
    now = now or _utcnow()
    today = now.date()
    email_cfg = config.channels.email
    max_bounce = getattr(email_cfg, "max_bounce_rate", DEFAULT_MAX_BOUNCE_RATE)
    try:
        max_bounce = float(max_bounce)
    except (TypeError, ValueError):
        max_bounce = DEFAULT_MAX_BOUNCE_RATE
    persona_email = getattr(getattr(config, "persona", None), "email", "") or ""
    resolve = _resolver(pool, persona_email)

    windows = sorted(set(SHORT_WINDOWS) | {VERDICT_WINDOW_DAYS})
    counts: dict[int, dict[str, dict[str, int]]] = {}
    unattributed = {"bounces": 0, "replies": 0}
    raw_by_domain: dict[str, set[str]] = {}
    for days in windows:
        since = _iso(now - timedelta(days=days))
        by_domain: dict[str, dict[str, int]] = {}
        for key, c in (await metrics.window_counts_by_mailbox(state.db_path, since)).items():
            owner = resolve(key)
            dom = _domain(owner)
            if not dom:
                if days == VERDICT_WINDOW_DAYS:
                    unattributed["bounces"] += c.get("bounces", 0)
                    unattributed["replies"] += c.get("replies", 0)
                continue
            raw_by_domain.setdefault(dom, set()).add(key)
            bucket = by_domain.setdefault(dom, {"sent": 0, "bounces": 0, "replies": 0})
            for metric in bucket:
                bucket[metric] += c.get(metric, 0)
        counts[days] = by_domain

    # Which mailboxes each domain has: the configured ones, plus any address
    # that sent from it (a mailbox since removed from the config).
    configured: dict[str, list] = {}
    if pool is not None:
        for mb in pool.mailboxes:
            if mb.domain:
                configured.setdefault(mb.domain, []).append(mb)
    first_sends = await _first_sends(state.db_path)
    first_by_domain: dict[str, date] = {}
    senders: dict[str, set[str]] = {}
    for key, day in first_sends.items():
        owner = resolve(key)
        dom = _domain(owner)
        if not dom:
            continue
        senders.setdefault(dom, set()).add(owner)
        if dom not in first_by_domain or day < first_by_domain[dom]:
            first_by_domain[dom] = day

    domains = set(configured) | set(counts[VERDICT_WINDOW_DAYS]) | set(first_by_domain)
    rows = []
    for dom in domains:
        starts = [(mb.warmup_start, "warmup_start") for mb in configured.get(dom, [])
                  if mb.warmup_start is not None and mb.warmup_start <= today]
        if dom in first_by_domain:
            starts.append((first_by_domain[dom], "first_send"))
        started_on, age_source = min(starts, key=lambda s: s[0]) if starts else (None, None)
        age_days = (today - started_on).days if started_on else None

        win = counts[VERDICT_WINDOW_DAYS].get(dom, {"sent": 0, "bounces": 0, "replies": 0})
        since = _iso(now - timedelta(days=VERDICT_WINDOW_DAYS))
        comp = await bounce_composition(state.db_path, raw_by_domain.get(dom, set()),
                                        since, win["bounces"])
        v = verdict(age_days=age_days, age_source=age_source, sent=win["sent"],
                    replies=win["replies"], bounces=comp, max_bounce_rate=max_bounce)
        listed = [mb.email for mb in configured.get(dom, [])]
        extra = sorted(senders.get(dom, set()) - set(listed))
        rows.append({
            "domain": dom,
            "mailboxes": listed + extra,
            "configured": bool(listed),
            "started_on": started_on.isoformat() if started_on else None,
            "age_days": age_days,
            "age_source": age_source,
            "windows": {
                f"{d}d": counts[d].get(dom, {"sent": 0, "bounces": 0, "replies": 0})
                for d in windows
            },
            "bounces_classified": comp.classified,
            "bounce_buckets": dict(comp.buckets),
            **v,
        })

    rows.sort(key=lambda r: (SEVERITY.index(r["verdict"]), r["domain"]))
    return {
        "generated_at": _iso(now),
        "window_days": VERDICT_WINDOW_DAYS,
        "short_windows": list(SHORT_WINDOWS),
        "thresholds": {**THRESHOLDS, "max_bounce_rate": max_bounce},
        "domains": rows,
        "unattributed": unattributed,
        "bounces_classified": any(r["bounces_classified"] for r in rows),
        "native": pool is not None,
    }


# ── Text rendering (mercury health) ──────────────────────────────────


def thresholds_text(report: dict) -> list[str]:
    t = report.get("thresholds") or THRESHOLDS
    return [
        f"Too young: under {t['min_age_days']} days of sending.",
        f"Not enough data: under {t['min_sends_reply']} sends for the reply rate, "
        f"under {t['min_sends_bounce']} for the bounce rate.",
        f"Keep: replies at {t['keep_reply_rate'] * 100:.0f}% or more after "
        f"{t['min_sends_reply']} sends.",
        f"Cancel candidate: 0 replies on {t['zero_reply_sends']}+ sends, under "
        f"{t['keep_reply_rate'] * 100:.0f}% after {t['min_sends_reply']}, or a burned "
        "bounce code.",
    ]


def format_report(report: dict, placement: dict | None = None) -> str:
    """The ``mercury health`` text."""
    days = report.get("window_days", VERDICT_WINDOW_DAYS)
    rows = report.get("domains") or []
    lines = ["", "  Deliverability health", "  " + "=" * 78]
    if not rows:
        lines += ["  No sending domain yet: nothing has been sent and no mailbox is configured.",
                  ""]
        return "\n".join(lines)

    w = max(14, *(len(r["domain"]) for r in rows))
    keys = [f"{d}d" for d in report.get("short_windows", SHORT_WINDOWS)] + [f"{days}d"]
    titles = [f"Last {k[:-1]} days" for k in keys]
    lines.append(f"  {'':<{w}} {'':>5}  " + "".join(f"{t:^18}" for t in titles))
    lines.append(f"  {'Domain':<{w}} {'Age':>5}  "
                 + "".join(f"{'sent':>6}{'rep':>6}{'bnc':>6}" for _ in keys) + "  Verdict")
    for r in rows:
        age = f"{r['age_days']}d" if r.get("age_days") is not None else "new"
        cells = "".join(
            f"{r['windows'][k]['sent']:>6}{r['windows'][k]['replies']:>6}"
            f"{r['windows'][k]['bounces']:>6}" for k in keys)
        lines.append(f"  {r['domain']:<{w}} {age:>5}  {cells}  {r['label']}")
    for r in rows:
        lines += ["", f"  {r['domain']}: {r['label']}", f"    {r['reason']}",
                  f"    Next: {r['next']}"]
        for flag in r.get("flags") or []:
            lines.append(f"    ! {flag['text']}")
        if r.get("mailboxes"):
            lines.append(f"    Mailboxes: {', '.join(r['mailboxes'])}")

    un = report.get("unattributed") or {}
    lines += ["", f"  The verdict reads the last {days} days. Age counts from the first send or "
                  "warm-up start."]
    lines += [f"  {t}" for t in thresholds_text(report)]
    if not report.get("bounces_classified"):
        lines.append("  Bounces aren't classified by SMTP code yet, so a burned domain shows up "
                     "only as missing replies.")
    if un.get("bounces") or un.get("replies"):
        lines.append(f"  Not charged to any domain (no matching send): {un.get('bounces', 0)} "
                     f"bounces, {un.get('replies', 0)} replies.")
    lines.append("")
    if placement:
        when = (placement.get("created_at") or "")[:10]
        lines.append(f"  Last placement test ({placement['run_id']}, {when}): "
                     f"{placement['summary']['label']}.")
        lines.append(f"    {placement['summary']['text']}")
    else:
        lines.append("  No placement test yet. Run: mercury mail placement --dry-run")
    lines.append("")
    return "\n".join(lines)
