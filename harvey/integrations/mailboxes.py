"""Mailbox pool: which address sends the next email, and how many it may send.

One mailbox carrying all cold volume is the fastest way to burn a domain.
The pool spreads sends over several mailboxes (usually one or two per
secondary domain), each with its own daily cap and an optional warm-up ramp:

    cap(day) = min(daily_cap, warmup_initial_cap + weeks_since_start * warmup_weekly_increase)

A thread stays on one mailbox. Step 1 picks a mailbox, and its follow-ups
and Harvey's replies go out from the same address. Otherwise a prospect would
get "Re:" mail from a stranger, and their answers would land in an inbox the
conversation never touched.

Without ``channels.email.mailboxes`` the pool wraps the single configured
provider (gmail, or the SMTP_* mailbox), so every deployment runs the same
code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from harvey.integrations.mail_provider import MailProvider, get_mail_provider

logger = logging.getLogger("harvey.mailboxes")


@dataclass
class Mailbox:
    email: str
    provider: MailProvider
    daily_cap: int
    warmup_start: date | None = None
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


def warmup_cap(daily_cap: int, warmup_start: date | None, day: date,
               initial: int, weekly_increase: int) -> int:
    """A mailbox's cap on ``day``: daily_cap once warm, the ramp before."""
    if warmup_start is None:
        return max(0, int(daily_cap))
    days = (day - warmup_start).days
    if days < 0:
        return 0  # warm-up has not started yet
    ramp = int(initial) + (days // 7) * int(weekly_increase)
    return max(0, min(int(daily_cap), ramp))


def planned_daily_capacity(config, day: date | None = None) -> int:
    """Sends per day the configuration allows, without touching credentials:
    max_daily_sends, further limited by the mailboxes' caps when rotating.
    Used for planning (how much to draft), not for the send decision."""
    email_cfg = config.channels.email
    max_daily = int(getattr(email_cfg, "max_daily_sends", 0) or 0)
    if not rotation_configured(config):
        return max_daily
    day = day or local_today(config)
    initial = getattr(email_cfg, "warmup_initial_cap", 5)
    weekly = getattr(email_cfg, "warmup_weekly_increase", 5)
    total = sum(
        warmup_cap(m.daily_cap, m.warmup_start, day, initial, weekly)
        for m in email_cfg.mailboxes if getattr(m, "enabled", True)
    )
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

    # ── construction ──

    @classmethod
    def single(cls, provider: MailProvider, daily_cap: int, email: str = "") -> "MailboxPool":
        """The pre-pool behaviour: one mailbox, capped by max_daily_sends."""
        return cls([Mailbox(email=(email or "").strip().lower(), provider=provider,
                            daily_cap=max(0, int(daily_cap)), legacy=True)])

    @classmethod
    def from_config(cls, config, env) -> "MailboxPool | None":
        """Build the pool for the configured native provider, or None
        (instantly / unknown provider)."""
        email_cfg = config.channels.email
        provider_name = (getattr(email_cfg, "provider", "") or "").strip().lower()
        max_daily = int(getattr(email_cfg, "max_daily_sends", 0) or 0)
        configured = [m for m in (getattr(email_cfg, "mailboxes", None) or [])
                      if getattr(m, "enabled", True)]

        if provider_name == "smtp" and configured:
            from harvey.integrations.smtp_mail import SmtpImapProvider

            legacy_login = (getattr(env, "smtp_username", "") or "").strip().lower()
            persona_email = (getattr(config.persona, "email", "") or "").strip().lower()
            mailboxes = [
                Mailbox(
                    email=m.email,
                    provider=SmtpImapProvider(config, env, mailbox=m),
                    daily_cap=m.daily_cap,
                    warmup_start=m.warmup_start,
                )
                for m in configured
            ]
            # Rows sent before this feature carry no mailbox. They went out
            # through SMTP_USERNAME, so that mailbox owns them (persona.email
            # as a fallback, else the first listed).
            for key in (legacy_login, persona_email):
                owner = next((mb for mb in mailboxes if key and mb.email == key), None)
                if owner:
                    owner.legacy = True
                    break
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
        mailbox tracking", i.e. the legacy mailbox."""
        key = (email or "").strip().lower()
        if not key:
            return self.legacy
        return next((mb for mb in self.mailboxes if mb.email == key), None)

    def configured(self) -> list[Mailbox]:
        return [mb for mb in self.mailboxes if mb.provider.is_configured()]

    def domains(self) -> set[str]:
        return {mb.domain for mb in self.mailboxes if mb.domain}

    # ── caps ──

    def cap_on(self, mb: Mailbox, day: date) -> int:
        return warmup_cap(mb.daily_cap, mb.warmup_start, day,
                          self.warmup_initial_cap, self.warmup_weekly_increase)

    def used(self, mb: Mailbox, sent_by_mailbox: dict[str, int]) -> int:
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
        instead of the warm one doing all the work."""
        best, best_key = None, None
        for idx, mb in enumerate(self.configured()):
            left = remaining.get(mb.email, 0)
            if left <= 0:
                continue
            cap = self.cap_on(mb, day) or 1
            key = (sent_this_cycle.get(mb.email, 0), -(left / cap), idx)
            if best_key is None or key < best_key:
                best, best_key = mb, key
        return best


def rotation_configured(config) -> bool:
    """True when the SMTP provider has a mailboxes list to rotate over."""
    email_cfg = config.channels.email
    provider_name = (getattr(email_cfg, "provider", "") or "").strip().lower()
    mailboxes = getattr(email_cfg, "mailboxes", None) or []
    return provider_name == "smtp" and any(getattr(m, "enabled", True) for m in mailboxes)


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


def mailbox_report(config, sent_by_mailbox: dict[str, int], has_secret,
                   smtp_username: str = "", day: date | None = None) -> dict:
    """What the dashboard shows about sending capacity. No network, no
    secrets: ``has_secret(name)`` only says whether an env var is set.

    Each row: email, name, daily_cap, cap_today, sent_24h, remaining,
    warmup_start, full_on (first day at daily_cap), stage, configured.
    """
    email_cfg = config.channels.email
    day = day or local_today(config)
    max_daily = int(getattr(email_cfg, "max_daily_sends", 0) or 0)
    initial = int(getattr(email_cfg, "warmup_initial_cap", 5) or 0)
    weekly = int(getattr(email_cfg, "warmup_weekly_increase", 5) or 0)
    total_sent = sum(sent_by_mailbox.values())
    rows: list[dict] = []

    if rotation_configured(config):
        listed = [m for m in email_cfg.mailboxes if getattr(m, "enabled", True)]
        login = (smtp_username or "").strip().lower()
        persona_email = (getattr(config.persona, "email", "") or "").strip().lower()
        legacy = next((m.email for key in (login, persona_email) for m in listed
                       if key and m.email == key), listed[0].email)
        for m in listed:
            cap = warmup_cap(m.daily_cap, m.warmup_start, day, initial, weekly)
            sent = int(sent_by_mailbox.get(m.email, 0))
            if m.email == legacy:
                sent += int(sent_by_mailbox.get("", 0))
            full_on = None
            if m.warmup_start is not None and weekly > 0 and m.daily_cap > initial:
                weeks = -(-(m.daily_cap - initial) // weekly)  # ceil
                full_on = (m.warmup_start + timedelta(days=7 * weeks)).isoformat()
            if m.warmup_start is not None and day < m.warmup_start:
                stage = "scheduled"
            elif cap < m.daily_cap:
                stage = "warming"
            else:
                stage = "warm"
            rows.append({
                "email": m.email,
                "name": m.name or getattr(config.persona, "name", ""),
                "daily_cap": m.daily_cap,
                "cap_today": cap,
                "sent_24h": sent,
                "remaining": max(0, cap - sent),
                "warmup_start": m.warmup_start.isoformat() if m.warmup_start else None,
                "full_on": full_on if stage != "warm" else None,
                "stage": stage,
                "configured": bool(has_secret(m.password_env)),
            })
    else:
        email = (getattr(config.persona, "email", "") or smtp_username or "").lower()
        rows.append({
            "email": email, "name": getattr(config.persona, "name", ""),
            "daily_cap": max_daily, "cap_today": max_daily, "sent_24h": total_sent,
            "remaining": max(0, max_daily - total_sent), "warmup_start": None,
            "full_on": None, "stage": "warm", "configured": None,
        })

    capacity = sum(r["cap_today"] for r in rows if r["configured"] is not False)
    return {
        "rotation": rotation_configured(config),
        "provider": getattr(email_cfg, "provider", ""),
        "mailboxes": rows,
        "capacity_today": min(max_daily, capacity),
        "max_daily_sends": max_daily,
        "sent_24h": total_sent,
        "require_approval": bool(getattr(email_cfg, "require_approval", True)),
        "auto_approve_followups": bool(getattr(email_cfg, "auto_approve_followups", False)),
        "spread_sends": bool(getattr(email_cfg, "spread_sends", False)),
    }
