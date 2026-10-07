"""Mailbox pool: which address sends the next email, and how many it may send.

One mailbox carrying all cold volume is the fastest way to burn a domain.
The pool spreads sends over several mailboxes (usually one or two per
secondary domain), each with its own daily cap and an optional warm-up ramp:

    cap(week) = min(daily_cap, warmup_initial_cap + weeks_since_start * warmup_weekly_increase,
                    2 * cap(week - 1))

The last term is a hard ceiling: no week may more than double the one before,
however large the configured weekly increase (a sudden jump is itself a
spam signal). The ramp is keyed on the inbox's age (days since warmup_start).

A thread stays on one mailbox. Step 1 picks a mailbox, and its follow-ups
and Mercury's replies go out from the same address. Otherwise a prospect would
get "Re:" mail from a stranger, and their answers would land in an inbox the
conversation never touched.

A mailbox with ``enabled: false`` takes no new threads, but its inbox is
still read and its threads still finish from it. Mail of a thread whose
mailbox was removed from the config is held by the sender, never re-routed:
nobody reads that inbox any more, so a reply or opt-out there would be lost.

Without ``channels.email.mailboxes`` the pool wraps the single configured
provider (gmail, or the SMTP_* mailbox), so every deployment runs the same
code path.

Health gates (see mercury/warmup.py) sit on top of the ramp: before each
drain the sender fills ``MailboxPool.gates`` from the last 7 days of sends
and bounces per mailbox. ``paused`` (a bounce spike, or a manual pause in the
dashboard) makes the mailbox's cap 0; ``hold`` keeps it at yesterday's cap.
A gate can only lower a cap, never raise it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from mercury.integrations.mail_provider import MailProvider, get_mail_provider

logger = logging.getLogger("mercury.mailboxes")


@dataclass
class Mailbox:
    email: str
    provider: MailProvider
    daily_cap: int
    warmup_start: date | None = None
    # False: finishes its threads and is still polled, but starts no new one.
    accepts_new: bool = True
    # Owns outbox rows recorded before mailbox tracking existed (mailbox = '').
    legacy: bool = False

    @property
    def domain(self) -> str:
        return self.email.split("@")[-1].lower() if "@" in self.email else ""


def local_today(config) -> date:
    """Today in the operator's timezone (quiet_hours.timezone)."""
    tz_name = "UTC"
    try:
        tz_name = config.usage.quiet_hours.timezone or "UTC"
    except AttributeError:
        pass
    try:
        import pytz

        return datetime.now(pytz.timezone(tz_name)).date()
    except Exception:
        return datetime.utcnow().date()


def ramp_week_cap(week: int, daily_cap: int, initial: int, weekly_increase: int) -> int:
    """The ramp's cap in inbox-age week ``week`` (0-based): the linear ramp
    clamped by daily_cap, and never more than double the previous week's cap
    (a zero previous week is exempt, so a ramp that starts at 0 can begin)."""
    daily_cap, initial, inc = max(0, int(daily_cap)), max(0, int(initial)), max(0, int(weekly_increase))
    cap = min(daily_cap, initial)
    for w in range(1, max(0, int(week)) + 1):
        nxt = min(daily_cap, initial + inc * w)
        cap = min(nxt, cap * 2) if cap > 0 else nxt
    return cap


def warmup_cap(daily_cap: int, warmup_start: date | None, day: date,
               initial: int, weekly_increase: int) -> int:
    """A mailbox's cap on ``day``: daily_cap once warm, the ramp before."""
    if warmup_start is None:
        return max(0, int(daily_cap))
    days = (day - warmup_start).days
    if days < 0:
        return 0  # warm-up has not started yet
    return ramp_week_cap(days // 7, daily_cap, initial, weekly_increase)


def ramp_full_weeks(daily_cap: int, initial: int, weekly_increase: int) -> int | None:
    """Whole weeks of age at which the ramp first reaches daily_cap; 0 when
    it starts there, None when it never gets there."""
    target = max(0, int(daily_cap))
    if target <= max(0, int(initial)):
        return 0
    if int(weekly_increase) <= 0:
        return None
    for week in range(1, 1000):
        if ramp_week_cap(week, target, initial, weekly_increase) >= target:
            return week
    return None


def full_volume_on(daily_cap: int, warmup_start: date | None,
                   initial: int, weekly_increase: int) -> date | None:
    """First day a warming mailbox reaches daily_cap; None if it never
    ramps (already warm, or a zero weekly increase)."""
    if warmup_start is None or daily_cap <= initial:
        return None
    weeks = ramp_full_weeks(daily_cap, initial, weekly_increase)
    if not weeks:
        return None
    return warmup_start + timedelta(days=7 * weeks)


def cap_source(mb: "Mailbox", day: date, initial: int, weekly_increase: int,
               gate: str = "") -> tuple[str, str]:
    """Why a mailbox's cap today is what it is: (key, plain sentence).
    Keys: paused, hold, scheduled, ramp, daily_cap."""
    if gate == "paused":
        return "paused", "Paused: sending is stopped until the inbox is resumed."
    if gate == "hold":
        return "hold", "Held at yesterday's level while the bounce rate is elevated."
    if mb.warmup_start is not None:
        if day < mb.warmup_start:
            return "scheduled", f"Warm-up starts {mb.warmup_start.isoformat()}; nothing sends before then."
        week = (day - mb.warmup_start).days // 7
        cap = ramp_week_cap(week, mb.daily_cap, initial, weekly_increase)
        if cap < mb.daily_cap:
            linear = min(int(mb.daily_cap), int(initial) + week * int(weekly_increase))
            if week > 0 and cap < linear:
                why = "no week may more than double the one before"
            else:
                why = f"{int(initial)} to start, +{int(weekly_increase)} a week"
            return "ramp", f"Warm-up week {week + 1}: {cap}/day ({why}) toward {mb.daily_cap}/day."
        return "daily_cap", f"Warm-up done: at its daily cap of {mb.daily_cap}/day."
    return "daily_cap", f"No warm-up date: fixed at its daily cap of {mb.daily_cap}/day."


# Operator guidance for cold email at small scale. Warnings only: the
# configured caps are never silently lowered.
DEFAULT_MAX_INBOXES_PER_DOMAIN = 2
DEFAULT_PROVIDER_CEILINGS = {"gmail": 30, "smtp": 15}
MIN_WARMUP_DAYS = 14


def inbox_limit_warnings(config, day: date | None = None) -> list[dict]:
    """Lifecycle limits the configured mailboxes break, as a list of
    ``{code, message, domain?, email?}``. Never changes a cap.

    - ``domain_inboxes``: more than ``max_inboxes_per_domain`` (2) on a domain.
    - ``cap_over_ceiling``: ``daily_cap`` above the provider's ceiling
      (``provider_daily_ceilings``: gmail 30, smtp 15).
    - ``young_inbox``: cold sends enabled less than 14 days after warmup_start.
    """
    email_cfg = config.channels.email
    listed = list(getattr(email_cfg, "mailboxes", None) or [])
    provider = (getattr(email_cfg, "provider", "") or "").strip().lower()
    if not listed or provider == "instantly":
        return []
    day = day or local_today(config)
    out: list[dict] = []

    limit = int(getattr(email_cfg, "max_inboxes_per_domain", DEFAULT_MAX_INBOXES_PER_DOMAIN) or 0)
    by_domain: dict[str, list[str]] = {}
    for m in listed:
        by_domain.setdefault(m.email.split("@")[-1], []).append(m.email)
    if limit > 0:
        for domain, emails in sorted(by_domain.items()):
            if len(emails) > limit:
                out.append({
                    "code": "domain_inboxes", "domain": domain,
                    "message": f"{domain} has {len(emails)} inboxes; more than {limit} on one "
                               f"domain risks flagging the whole domain.",
                })

    configured_ceilings = getattr(email_cfg, "provider_daily_ceilings", None)
    if configured_ceilings is None:
        configured_ceilings = DEFAULT_PROVIDER_CEILINGS
    ceilings = {str(k).lower(): int(v) for k, v in configured_ceilings.items()}
    ceiling = ceilings.get(provider, 0)
    for m in listed:
        if ceiling > 0 and m.daily_cap > ceiling:
            out.append({
                "code": "cap_over_ceiling", "email": m.email,
                "message": f"{m.email} daily_cap {m.daily_cap} is above the {provider} ceiling "
                           f"of {ceiling}/day.",
            })
        start = m.warmup_start
        if m.enabled and start is not None and 0 <= (day - start).days < MIN_WARMUP_DAYS:
            out.append({
                "code": "young_inbox", "email": m.email,
                "message": f"{m.email} started warm-up {(day - start).days} day(s) ago; "
                           f"wait {MIN_WARMUP_DAYS} days before cold mail (disable it until then).",
            })
    return out


def rotation_configured(config) -> bool:
    """True when the SMTP provider has a mailboxes list to rotate over."""
    email_cfg = config.channels.email
    provider_name = (getattr(email_cfg, "provider", "") or "").strip().lower()
    return provider_name == "smtp" and bool(getattr(email_cfg, "mailboxes", None))


def planned_daily_capacity(config, day: date | None = None, env=None) -> int:
    """New threads per day the configuration allows, without opening a
    connection: max_daily_sends, further limited by the caps of mailboxes
    that have credentials and take new threads. Used for planning (how much
    to draft, whether the cap is reached), not for the send decision."""
    email_cfg = config.channels.email
    max_daily = int(getattr(email_cfg, "max_daily_sends", 0) or 0)
    if not rotation_configured(config):
        return max_daily
    if env is None:
        from mercury.config import load_env

        env = load_env()
    pool = MailboxPool.from_config(config, env)
    day = day or local_today(config)
    total = sum(pool.cap_on(mb, day) for mb in pool.configured() if mb.accepts_new)
    return min(max_daily, total)


class MailboxPool:
    def __init__(
        self,
        mailboxes: list[Mailbox],
        warmup_initial_cap: int = 5,
        warmup_weekly_increase: int = 5,
    ):
        if not mailboxes:
            raise ValueError("a mailbox pool needs at least one mailbox")
        self.mailboxes = mailboxes
        self.warmup_initial_cap = max(0, int(warmup_initial_cap))
        self.warmup_weekly_increase = max(0, int(warmup_weekly_increase))
        if not any(mb.legacy for mb in mailboxes):
            mailboxes[0].legacy = True
        # One mailbox without a rotation list: whatever address a row
        # recorded, it went out through (and is answered in) this inbox.
        self.single_inbox = False
        # email -> "paused" | "hold", filled by warmup.apply_health().
        self.gates: dict[str, str] = {}

    # ── construction ──

    @classmethod
    def single(cls, provider: MailProvider, daily_cap: int, email: str = "") -> "MailboxPool":
        """The pre-pool behaviour: one mailbox, capped by max_daily_sends."""
        pool = cls([Mailbox(email=(email or "").strip().lower(), provider=provider,
                            daily_cap=max(0, int(daily_cap)), legacy=True)])
        pool.single_inbox = True
        return pool

    @classmethod
    def from_config(cls, config, env) -> "MailboxPool | None":
        """Build the pool for the configured native provider, or None
        (instantly / unknown provider)."""
        email_cfg = config.channels.email
        provider_name = (getattr(email_cfg, "provider", "") or "").strip().lower()
        max_daily = int(getattr(email_cfg, "max_daily_sends", 0) or 0)
        listed = list(getattr(email_cfg, "mailboxes", None) or [])

        if listed and provider_name != "smtp":
            logger.warning(
                f"Mailboxes: channels.email.mailboxes is only used with the smtp "
                f"provider; ignoring it for '{provider_name}'."
            )

        if provider_name == "smtp" and listed:
            from mercury.integrations.smtp_mail import SmtpImapProvider

            mailboxes = [
                Mailbox(
                    email=m.email,
                    provider=SmtpImapProvider(config, env, mailbox=m),
                    daily_cap=m.daily_cap,
                    warmup_start=m.warmup_start,
                    accepts_new=bool(getattr(m, "enabled", True)),
                )
                for m in listed
            ]
            # Rows sent before this feature carry no mailbox. They went out
            # From persona.email (or the SMTP login when that was empty).
            persona_email = (getattr(config.persona, "email", "") or "").strip().lower()
            login = (getattr(env, "smtp_username", "") or "").strip().lower()
            owner = next((mb for key in (persona_email, login) for mb in mailboxes
                          if key and mb.email == key), None)
            if owner:
                owner.legacy = True
            else:
                logger.warning(
                    f"Mailboxes: neither persona.email ({persona_email or 'empty'}) nor "
                    f"SMTP_USERNAME is in channels.email.mailboxes; threads started "
                    f"before rotation continue from {mailboxes[0].email}."
                )
            return cls(
                mailboxes,
                warmup_initial_cap=getattr(email_cfg, "warmup_initial_cap", 5),
                warmup_weekly_increase=getattr(email_cfg, "warmup_weekly_increase", 5),
            )

        provider = get_mail_provider(config, env)
        if provider is None:
            return None
        return cls.single(provider, max_daily, getattr(config.persona, "email", "") or "")

    # ── lookup ──

    @property
    def primary(self) -> Mailbox:
        return self.mailboxes[0]

    @property
    def legacy(self) -> Mailbox:
        return next(mb for mb in self.mailboxes if mb.legacy)

    def resolve(self, email: str | None) -> Mailbox | None:
        """The mailbox a stored outbox value refers to. '' means "sent before
        mailbox tracking", i.e. the legacy mailbox. None: not in the pool."""
        key = (email or "").strip().lower()
        if not key or self.single_inbox:
            return self.legacy
        return next((mb for mb in self.mailboxes if mb.email == key), None)

    def configured(self) -> list[Mailbox]:
        """Mailboxes with credentials: these are polled and may send."""
        return [mb for mb in self.mailboxes if mb.provider.is_configured()]

    def domains(self) -> set[str]:
        return {mb.domain for mb in self.mailboxes if mb.domain}

    # ── caps ──

    def base_cap_on(self, mb: Mailbox, day: date) -> int:
        """The configured ramp alone, before any health gate."""
        return warmup_cap(mb.daily_cap, mb.warmup_start, day,
                          self.warmup_initial_cap, self.warmup_weekly_increase)

    def cap_on(self, mb: Mailbox, day: date) -> int:
        """The cap the sender enforces: the ramp, lowered by a health gate."""
        cap = self.base_cap_on(mb, day)
        gate = self.gates.get(mb.email)
        if gate == "paused":
            return 0
        if gate == "hold":
            # Yesterday's ramp value; on the first ramp day that is day one.
            prev = day - timedelta(days=1)
            if mb.warmup_start is not None and prev < mb.warmup_start:
                prev = mb.warmup_start
            cap = min(cap, self.base_cap_on(mb, prev))
        return cap

    def used(self, mb: Mailbox, sent_by_mailbox: dict[str, int]) -> int:
        if self.single_inbox:
            return sum(int(v) for v in sent_by_mailbox.values())
        n = int(sent_by_mailbox.get(mb.email, 0))
        if mb.legacy and mb.email:
            n += int(sent_by_mailbox.get("", 0))
        return n

    def remaining(self, sent_by_mailbox: dict[str, int], day: date) -> dict[str, int]:
        """Sends left in the rolling day, per configured mailbox."""
        return {
            mb.email: max(0, self.cap_on(mb, day) - self.used(mb, sent_by_mailbox))
            for mb in self.configured()
        }

    def capacity_on(self, day: date) -> int:
        """Total daily sends the configured mailboxes allow on ``day``."""
        return sum(self.cap_on(mb, day) for mb in self.configured())

    # ── selection ──

    def pick(
        self,
        remaining: dict[str, int],
        sent_this_cycle: dict[str, int],
        day: date,
    ) -> Mailbox | None:
        """Mailbox for a new thread: fewest sends this cycle, then the largest
        share of its daily cap still unused, then list order. A warming
        mailbox (cap 5) and a warm one (cap 30) both drain at their own pace
        instead of the warm one doing all the work. Disabled mailboxes take
        no new threads."""
        best, best_key = None, None
        for idx, mb in enumerate(self.configured()):
            left = remaining.get(mb.email, 0)
            if left <= 0 or not mb.accepts_new:
                continue
            cap = self.cap_on(mb, day) or 1
            key = (sent_this_cycle.get(mb.email, 0), -(left / cap), idx)
            if best_key is None or key < best_key:
                best, best_key = mb, key
        return best


def build_rotation_pool(config, env) -> MailboxPool | None:
    """The rotation pool when mailboxes are configured, else None (the agent
    then wraps its single provider). Never raises: a broken mailbox config
    should hold the outbox with a log line, not crash the heartbeat."""
    if not rotation_configured(config):
        return None
    try:
        return MailboxPool.from_config(config, env)
    except Exception as e:  # pragma: no cover - defensive
        logger.error(f"Mailboxes: could not build the mailbox pool: {e}")
        return None


def mailbox_report(config, pool: MailboxPool | None, sent_by_mailbox: dict[str, int],
                   day: date | None = None) -> dict:
    """What the dashboard shows about sending capacity, computed with the
    same pool and the same rules the sender enforces. No network, no
    secrets: "configured" is the provider's own credential check.
    """
    email_cfg = config.channels.email
    day = day or local_today(config)
    max_daily = int(getattr(email_cfg, "max_daily_sends", 0) or 0)
    total_sent = sum(int(v) for v in sent_by_mailbox.values())
    base = {
        "rotation": rotation_configured(config),
        "provider": getattr(email_cfg, "provider", ""),
        "max_daily_sends": max_daily,
        "sent_24h": total_sent,
        "require_approval": bool(getattr(email_cfg, "require_approval", True)),
        "auto_approve_followups": bool(getattr(email_cfg, "auto_approve_followups", False)),
        "spread_sends": bool(getattr(email_cfg, "spread_sends", False)),
        "limit_warnings": inbox_limit_warnings(config, day),
    }
    if pool is None:  # instantly: the outbox numbers mean nothing here
        return {**base, "mailboxes": [], "legacy_email": "", "capacity_today": 0,
                "capped_by_global": False}

    global_left = max(0, max_daily - total_sent)
    rows: list[dict] = []
    known: set[str] = set()
    for mb in pool.mailboxes:
        configured = mb.provider.is_configured()
        cap = pool.cap_on(mb, day)
        base_cap = pool.base_cap_on(mb, day)
        gate = pool.gates.get(mb.email, "")
        sent = pool.used(mb, sent_by_mailbox)
        known.add(mb.email)
        if mb.legacy:
            known.add("")
        full_on = full_volume_on(mb.daily_cap, mb.warmup_start,
                                 pool.warmup_initial_cap, pool.warmup_weekly_increase)
        if gate == "paused":
            stage = "paused"
        elif mb.warmup_start is not None and day < mb.warmup_start:
            stage = "scheduled"
        elif base_cap < mb.daily_cap:
            stage = "warming" if full_on else "fixed"
        else:
            stage = "warm"
        from_name = getattr(mb.provider, "from_name", None)
        src, src_why = cap_source(mb, day, pool.warmup_initial_cap,
                                  pool.warmup_weekly_increase, gate)
        rows.append({
            "cap_source": src,
            "cap_reason": src_why,
            "email": mb.email,
            "name": (from_name if isinstance(from_name, str) else "")
                    or getattr(config.persona, "name", ""),
            "daily_cap": mb.daily_cap,
            "cap_today": cap,
            "base_cap_today": base_cap,
            "gate": gate,
            "sent_24h": sent,
            # What the sender would really still send from it today.
            "remaining": min(max(0, cap - sent), global_left) if configured else 0,
            "warmup_start": mb.warmup_start.isoformat() if mb.warmup_start else None,
            "full_on": full_on.isoformat() if (full_on and stage != "warm") else None,
            "stage": stage,
            "accepts_new": mb.accepts_new,
            "configured": configured,
            "legacy": mb.legacy,
        })

    # Sends from mailboxes no longer in the config still count globally.
    other = sum(int(v) for k, v in sent_by_mailbox.items() if k not in known)
    if other:
        rows.append({
            "email": "", "name": "removed mailboxes", "daily_cap": 0, "cap_today": 0,
            "base_cap_today": 0, "gate": "", "sent_24h": other, "remaining": 0, "warmup_start": None, "full_on": None,
            "stage": "removed", "accepts_new": False, "configured": None, "legacy": False,
        })

    mailbox_capacity = pool.capacity_on(day)
    return {
        **base,
        "mailboxes": rows,
        "legacy_email": pool.legacy.email,
        "capacity_today": min(max_daily, mailbox_capacity),
        "capped_by_global": max_daily < mailbox_capacity,
    }
