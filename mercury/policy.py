"""Contact policy: exclusions and per-company limits, in one place.

The Sender, imports, staging and the dashboard all ask this module the same
questions, so an email is never blocked for one reason and explained with
another:

  * Is this address excluded? (an opt-out, a bounce, or a rule you added)
  * Which company does this contact count against, if any?
  * Why is this queued email not going out, and what can be done about it?

The rules themselves live in SQLite (``suppressions``, ``company_holds``) and
the checks that must be atomic run inside ``StateManager.claim_for_send``.

Company identity is deliberately conservative. A contact belongs to a company
when its ``company_id`` names a known company record, or when its email
domain is the domain of a known company. A shared mail provider (gmail.com,
outlook.com, ...) never makes one company, and a contact with no known company
is shown as "company unknown" and gets no company limit, rather than a limit
computed from a guess.
"""

from __future__ import annotations

from dataclasses import dataclass

from mercury.collectors.discover import normalize_domain
from mercury.csv_import import FREE_MAIL, normalize_email, valid_email
from mercury.state import SOURCE_LABELS, describe_rule

# Who may add a rule, as recorded in suppressions.source. 'opt_out' and
# 'bounce' come from the recipient's own mail; only Mercury records them.
SOURCES = ("opt_out", "bounce", "manual", "import")
# Rules a person must lift on purpose (never by import, never in bulk).
PROTECTED_SOURCES = ("opt_out",)


def normalize_rule_value(kind: str, raw: str) -> str:
    """The stored form of a rule: a lowercase address, or a bare lowercase
    hostname with no scheme, path, port or leading www/@. '' when invalid."""
    raw = (raw or "").strip()
    if kind == "email":
        email = normalize_email(raw)
        return email if valid_email(email) else ""
    domain = normalize_domain(raw.lstrip("@").split("@")[-1])
    return domain if domain and "." in domain else ""


def email_domain(email: str) -> str:
    email = (email or "").strip().lower()
    return email.rsplit("@", 1)[1] if "@" in email else ""


def is_shared_provider(domain: str) -> bool:
    return (domain or "").lower() in FREE_MAIL


@dataclass
class CompanyLimits:
    max_new_per_day: int = 0
    max_active: int = 0
    pause_on_reply: bool = True

    @classmethod
    def from_config(cls, config) -> "CompanyLimits":
        email = getattr(getattr(config, "channels", None), "email", None)
        return cls(
            max_new_per_day=int(getattr(email, "max_new_contacts_per_company_per_day", 0) or 0),
            max_active=int(getattr(email, "max_active_contacts_per_company", 0) or 0),
            pause_on_reply=bool(getattr(email, "pause_company_on_reply", True)),
        )


@dataclass
class Verdict:
    """Why an email is or is not going out. ``tone`` maps to a dashboard badge."""
    code: str              # ok, excluded, company_hold, company_daily_limit, ...
    reason: str = ""
    action: str = ""       # what the operator can do about it
    tone: str = "good"

    def as_dict(self) -> dict:
        return {"code": self.code, "reason": self.reason, "action": self.action,
                "tone": self.tone}


def capability(config) -> dict:
    """Which controls the configured provider enforces. The legacy Instantly
    path hands whole sequences to Instantly, so Mercury can only apply
    exclusions when it adds leads; it cannot hold or limit a send there."""
    provider = getattr(getattr(getattr(config, "channels", None), "email", None),
                       "provider", "")
    from mercury.integrations.mail_provider import NATIVE_PROVIDERS
    native = provider in NATIVE_PROVIDERS
    return {
        "provider": provider,
        "company_limits": native,
        "send_time_exclusions": native,
        "note": "" if native else (
            "Instantly sends the sequence itself, so Mercury applies exclusions only when "
            "it adds leads, and cannot enforce company limits or reply holds. Switch "
            "channels.email.provider to gmail or smtp for both."),
    }


class ContactPolicy:
    def __init__(self, state, config=None):
        self.state = state
        self.limits = CompanyLimits.from_config(config) if config is not None else CompanyLimits()

    # ── Exclusions ──

    async def exclusion_for(self, email: str) -> dict | None:
        """The rule that excludes this address, or None. Opt-outs win ties."""
        rules = await self.state.find_suppressions(email)
        return rules[0] if rules else None

    # ── Company identity ──

    async def company_for(self, prospect) -> str:
        """The company id this contact counts against, or '' when unknown."""
        if prospect is None:
            return ""
        company_id = getattr(prospect, "company_id", "") or ""
        if company_id and await self.state.get_company(company_id):
            return company_id
        domain = email_domain(getattr(prospect, "email", ""))
        if domain and not is_shared_provider(domain):
            company = await self.state.get_company_by_domain(domain)
            if company:
                return company.id
        return ""

    # ── Explanations ──

    async def explain(self, item: dict, prospect=None) -> Verdict:
        """Why a queued email will or will not leave, in plain words. Read
        only: the Sender re-checks all of this under the write lock."""
        status = item.get("status")
        if status == "blocked":
            rule = await self.exclusion_for(item.get("to_email", ""))
            if rule:
                return Verdict("excluded", f"Excluded: {describe_rule(rule)}.",
                               _lift_hint(rule), "bad")
            return Verdict("blocked", "Blocked by an exclusion that has since been removed.",
                           "Send it back to review to decide again.", "waiting")
        if status not in ("pending_review", "approved"):
            return Verdict("ok")
        rule = await self.exclusion_for(item.get("to_email", ""))
        if rule:
            return Verdict("excluded", f"Excluded: {describe_rule(rule)}.",
                           _lift_hint(rule), "bad")
        if item.get("kind") != "sequence":
            return Verdict("ok")
        pause = await self.state.get_active_pause(item.get("prospect_id", ""))
        if pause:
            return Verdict("ooo_pause", _pause_reason(pause),
                           "Correct the return date or resume them under Away in the Outbox.",
                           "waiting")
        if prospect is None:
            prospect = await self.state.get_prospect(item.get("prospect_id", ""))
        company_id = await self.company_for(prospect)
        if not company_id:
            return Verdict("company_unknown",
                           "Company unknown, so no company limit applies.", "", "idle")
        hold = await self.state.get_company_hold(company_id)
        if hold:
            replier = await self.state.get_prospect(hold.get("prospect_id") or "")
            hold["prospect_email"] = replier.email if replier else ""
            return Verdict("company_hold", _hold_reason(hold),
                           "Resume the company in Exclusions to let cold mail go again.",
                           "waiting")
        if int(item.get("step") or 1) != 1 or not (self.limits.max_new_per_day
                                                  or self.limits.max_active):
            return Verdict("ok")
        usage = await self.state.company_contact_usage(
            company_id, exclude_campaign=item.get("campaign_id") or "",
            exclude_prospect=item.get("prospect_id") or "")
        limits = self.limits
        if limits.max_new_per_day and usage["new_today"] >= limits.max_new_per_day:
            return Verdict(
                "company_daily_limit",
                f"Waiting: {usage['new_today']} of {limits.max_new_per_day} new contacts at "
                "this company in the last 24 hours.",
                "It goes out once the 24 hours roll over.", "waiting")
        if limits.max_active and usage["active"] >= limits.max_active:
            return Verdict(
                "company_active_limit",
                f"Waiting: {usage['active']} of {limits.max_active} contacts at this company "
                "are already in a sequence.",
                "It goes out when one of those sequences finishes or you end one.", "waiting")
        return Verdict("ok")


def _pause_reason(pause: dict) -> str:
    if pause.get("review_state") == "scheduled" and pause.get("resume_at"):
        return (f"Out of office: waits until {pause['resume_at'][:16].replace('T', ' ')} UTC, "
                "when they are back.")
    return "Out of office with no clear return date: waits until you set one or resume them."


def _hold_reason(hold: dict) -> str:
    if hold.get("reason") == "reply":
        who = hold.get("prospect_email") or "someone at this company"
        return f"Held: {who} replied, so cold mail to their colleagues is paused."
    note = (hold.get("note") or "").strip().rstrip(".")
    return "Held: you paused cold mail to this company." + (f" {note}." if note else "")


def _lift_hint(rule: dict) -> str:
    if rule["source"] in PROTECTED_SOURCES:
        return "They asked not to be contacted. Only lift this if they asked to hear from you again."
    if rule["source"] == "bounce":
        return "The address bounced. Lift the rule only if you know it works now."
    return "Remove the rule in Exclusions, then send this email back to review."


__all__ = [
    "CompanyLimits", "ContactPolicy", "PROTECTED_SOURCES", "SOURCES", "SOURCE_LABELS",
    "Verdict", "capability", "describe_rule", "email_domain", "is_shared_provider",
    "normalize_rule_value",
]
