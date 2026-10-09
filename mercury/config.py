"""Configuration loader for Mercury. Reads mercury.yaml + .env."""

import logging
import os
from datetime import date as _date
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator, model_validator

from mercury.paths import PROJECT_ROOT

logger = logging.getLogger("mercury.config")


class ConfigError(Exception):
    """Raised when Mercury's configuration is missing or invalid."""


class ConfigFileNotFoundError(ConfigError, FileNotFoundError):
    """Config file is missing. Subclasses FileNotFoundError for
    backward compatibility with existing callers/tests."""


class PersonaConfig(BaseModel):
    name: str
    company: str
    role: str
    email: str
    linkedin: str
    tone: str


class OfferConfig(BaseModel):
    primary: str = ""
    entry: str = ""
    goal: str = "book_call"  # book_call, start_trial, get_reply
    booking_method: str = "calendar_link"  # calendar_link, suggest_times, ask_preference
    booking_url: str = ""
    meeting_duration: str = "15 minutes"
    meeting_owner: str = ""


class ProductConfig(BaseModel):
    name: str
    description: str
    pricing: str
    key_benefits: list[str]
    objection_responses: dict[str, str]
    offer: OfferConfig = OfferConfig()


class MarketConfig(BaseModel):
    """One market for discovery: its own places, terms and language.

    "ferretería" is searched in Santo Domingo and "plumber" in Tampa, instead
    of every term in every city. `industries`/`geography` still describe the
    ICP for scoring and writing.
    """
    name: str
    places: list[str]
    terms: list[str]
    lang: str = "en"


class ICPConfig(BaseModel):
    industries: list[str]
    company_size: str
    titles: list[str]
    geography: list[str]
    # Role keywords that indicate a company is in-market right now (a company
    # hiring a "Head of Growth" is buying growth tooling). Empty → falls back
    # to `titles`. Used for careers-page scanning and job-board discovery.
    hiring_signals: list[str] = []
    # Discovery needs a radius, not a place name. Maps each entry in
    # `geography` to "lat,lng,radius_km" — e.g.
    #   "Denver, CO": "39.7392,-104.9903,50"
    # Without one, listings providers can only match on the business name.
    geo_coordinates: dict[str, str] = {}
    # Market-aware discovery (see MarketConfig). Empty → industries x geography.
    markets: list[MarketConfig] = []


class MailboxConfig(BaseModel):
    """One sending mailbox (SMTP provider only).

    Several mailboxes on secondary domains spread cold volume so no single
    address carries it. Host/port default to SMTP_HOST / SMTP_PORT /
    IMAP_HOST / IMAP_PORT from .env; the login defaults to ``email``. The
    password is read from the env var named in ``password_env``. Passwords
    never live in YAML.
    """
    email: str
    # From display name. Empty -> persona.name.
    name: str = ""
    username: str = ""
    # Env var holding this mailbox's password, e.g. MAILBOX_PASSWORD or
    # SMTP_PASSWORD (the legacy single mailbox).
    password_env: str = "SMTP_PASSWORD"
    smtp_host: str = ""
    smtp_port: int = 0
    imap_host: str = ""
    imap_port: int = 0
    imap_username: str = ""
    # Preserve a legacy inbox with separate IMAP credentials when rotation
    # is enabled from the dashboard. Empty uses its SMTP password.
    imap_password_env: str = ""
    # Steady-state ceiling once warm-up has run its course. 15 is the smtp
    # provider ceiling (provider_daily_ceilings), so the default never warns.
    daily_cap: int = 15
    # First day this mailbox sent cold mail. The cap starts at
    # channels.email.warmup_initial_cap and grows weekly from here. Leave
    # empty for a mailbox that is already warm. A date in the future means
    # "not yet": the mailbox sends nothing until then.
    warmup_start: _date | None = None
    # false = start no NEW threads here. Its inbox is still read and its
    # existing threads still finish from it, so replies and opt-outs are
    # never missed. Remove the entry only once its threads are done: mail of
    # a thread whose mailbox is gone is held, never re-routed.
    enabled: bool = True

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if "@" not in v or v.startswith("@") or v.endswith("@"):
            raise ValueError(f"'{v}' is not an email address")
        return v

    @field_validator("daily_cap")
    @classmethod
    def _cap_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("daily_cap must be >= 0")
        return v


class PlacementSeedConfig(BaseModel):
    """A seed inbox you own that the placement test sends to and reads.

    Mercury reads it over IMAP to see which folder each test email landed
    in. ``provider`` (gmail, outlook, yahoo, other) and ``imap_host`` are
    guessed from the address when left empty; set ``provider: gmail`` for a
    Google Workspace seed on its own domain so Primary and Promotions are
    told apart. The password (an app password) is read from the env var in
    ``password_env``, which must start with PLACEMENT_ or MAILBOX_. Leave it
    empty to read the seed yourself and record the folder with
    ``mercury mail placement mark``.
    """
    email: str
    provider: str = ""
    password_env: str = ""
    username: str = ""
    imap_host: str = ""
    imap_port: int = 993

    @field_validator("email")
    @classmethod
    def _seed_email(cls, v: str) -> str:
        return _address(v)


def _address(v: str) -> str:
    v = (v or "").strip().lower()
    if "@" not in v or v.startswith("@") or v.endswith("@"):
        raise ValueError(f"'{v}' is not an email address")
    return v


class PlacementControlConfig(BaseModel):
    """A known-good personal mailbox (usually a personal Gmail) that sends
    the same text as the control. Its SMTP password comes from the env var
    in ``password_env`` (PLACEMENT_* or MAILBOX_*)."""
    email: str
    name: str = ""
    password_env: str = ""
    username: str = ""
    smtp_host: str = ""
    smtp_port: int = 587

    @field_validator("email")
    @classmethod
    def _control_email(cls, v: str) -> str:
        return _address(v)


class PlacementConfig(BaseModel):
    """The inbox placement test (``mercury mail placement``)."""
    seeds: list[PlacementSeedConfig] = []
    control: PlacementControlConfig | None = None
    # How long to keep looking for the test emails in the seed inboxes.
    wait_seconds: int = 180


class EmailChannelConfig(BaseModel):
    enabled: bool = True
    # "instantly" (legacy), "gmail" (Gmail/Workspace via API — recommended),
    # or "smtp" (any SMTP+IMAP mailbox: AgentMail, Fastmail, ...)
    provider: str = "instantly"
    max_daily_sends: int = 50
    # When True, also send to catch-all ("risky") domains, not just verified
    # mailboxes. Off by default — catch-alls accept everything, so a bad
    # guess still bounces.
    send_to_risky: bool = False
    # Copilot mode (native providers): every outgoing email waits in the
    # outbox for your approval (dashboard → Outbox, or `mercury outbox`).
    # Set false for full autopilot once you trust the output.
    require_approval: bool = True
    # Kill switch: pause all sending when bounces exceed this fraction of
    # sent mail (checked once 50 emails have gone out; NOISE bounces such as
    # "mailbox full" do not count). 0 disables this rate check. Sender and
    # reputation blocks (5.7.x) have their own trigger, see mercury/bounces.py.
    # 2% is where Gmail reputation damage starts; 5% was already past it.
    max_bounce_rate: float = 0.02
    # SMTP only: rotate sends across these mailboxes. Empty keeps the single
    # SMTP_USERNAME mailbox from .env. max_daily_sends still caps the total.
    mailboxes: list[MailboxConfig] = []
    # Warm-up ramp for mailboxes with a warmup_start: the daily cap starts
    # here and rises by warmup_weekly_increase every 7 days, up to daily_cap.
    warmup_initial_cap: int = 5
    warmup_weekly_increase: int = 5
    # Inbox lifecycle limits. Breaking one is a warning (CLI, startup log and
    # the dashboard Mailboxes tab), or a refusal to start under
    # `mercury run --strict`. Caps are never lowered silently. Set a value to
    # 0 (or drop a provider) to switch that check off.
    max_inboxes_per_domain: int = 2
    # Safe per-inbox daily_cap by provider, overridable per deployment.
    provider_daily_ceilings: dict[str, int] = {"gmail": 30, "smtp": 15}
    # With require_approval on, approving a first email also approves its
    # follow-ups (steps 2+), so a sequence you signed off on is not stuck
    # waiting for a second and third click. Replies still need approval.
    auto_approve_followups: bool = False
    # Native providers: send sequence follow-ups (steps 2+) as replies in
    # the opener's thread (In-Reply-To / References, Gmail threadId, and a
    # "Re: <first subject>" subject). Off sends each step as a new email
    # with its own subject.
    thread_followups: bool = True
    # Pace the day's remaining sends evenly over the cycles left before
    # quiet hours, instead of sending up to MAX_SENDS_PER_CYCLE at once.
    spread_sends: bool = False
    # Out-of-office replies: resume the sequence this many business days
    # after the return date they gave. 0 resumes the morning they are back.
    # A date you set by hand on the Outbox tab is used as is.
    ooo_resume_buffer_days: int = 0
    # Seed inboxes and a control sender for `mercury mail placement`.
    placement: PlacementConfig = PlacementConfig()
    # Company contact policy (native providers). 0 = no limit. A company is
    # a known company record: a prospect's company_id, or a company whose
    # domain is the email's domain. Shared providers (gmail.com, ...) never
    # make one company, and a contact with no known company gets no limit.
    # New contacts per company: first emails in a rolling 24 hours, the same
    # window as max_daily_sends, so it does not depend on a timezone.
    max_new_contacts_per_company_per_day: int = 0
    # Contacts per company with an unfinished cold sequence (first email
    # sent, later steps still queued). A paused sequence keeps its slot
    # until its remaining steps are rejected or cancelled.
    max_active_contacts_per_company: int = 0
    # When someone at a company replies (a person, not an auto-responder or
    # a bounce), hold cold mail to their colleagues until you resume it.
    pause_company_on_reply: bool = True

    @field_validator("max_daily_sends", "max_new_contacts_per_company_per_day",
                     "max_active_contacts_per_company")
    @classmethod
    def _sends_non_negative(cls, v: int, info) -> int:
        if v < 0:
            raise ValueError(f"{info.field_name} must be >= 0")
        return v

    @field_validator("ooo_resume_buffer_days")
    @classmethod
    def _buffer_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("ooo_resume_buffer_days must be >= 0")
        return v

    @field_validator("warmup_initial_cap", "warmup_weekly_increase")
    @classmethod
    def _warmup_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("warm-up values must be >= 0")
        return v

    @field_validator("max_inboxes_per_domain")
    @classmethod
    def _inboxes_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_inboxes_per_domain must be >= 0")
        return v

    @field_validator("mailboxes")
    @classmethod
    def _unique_mailboxes(cls, v: list[MailboxConfig]) -> list[MailboxConfig]:
        seen: set[str] = set()
        for mb in v:
            if mb.email in seen:
                raise ValueError(f"mailbox {mb.email} is listed twice")
            seen.add(mb.email)
        return v


class LinkedInChannelConfig(BaseModel):
    enabled: bool = True
    max_daily_connections: int = 20
    max_daily_messages: int = 10


class ChannelsConfig(BaseModel):
    email: EmailChannelConfig = EmailChannelConfig()
    linkedin: LinkedInChannelConfig = LinkedInChannelConfig()


class QuietHoursConfig(BaseModel):
    start: str = "22:00"
    end: str = "07:00"
    timezone: str = "America/New_York"

    @field_validator("start", "end")
    @classmethod
    def _valid_time(cls, v: str) -> str:
        try:
            _time.fromisoformat(v)
        except ValueError:
            raise ValueError(
                f"'{v}' is not a valid time. Use 24h HH:MM format, e.g. '22:00'."
            )
        return v

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        import pytz

        if v not in pytz.all_timezones_set:
            raise ValueError(
                f"'{v}' is not a valid timezone. Use an IANA name like 'America/New_York'."
            )
        return v


class UsageConfig(BaseModel):
    max_daily_claude_percent: float = 80.0
    heartbeat_interval_minutes: int = 15
    quiet_hours: QuietHoursConfig = QuietHoursConfig()

    @field_validator("max_daily_claude_percent")
    @classmethod
    def _valid_percent(cls, v: float) -> float:
        if not 0 < v <= 100:
            raise ValueError("max_daily_claude_percent must be between 0 and 100")
        return v

    @field_validator("heartbeat_interval_minutes")
    @classmethod
    def _valid_interval(cls, v: int) -> int:
        if v < 1:
            raise ValueError("heartbeat_interval_minutes must be at least 1")
        return v


class ComplianceConfig(BaseModel):
    """Legal footer the sender appends to every outbound sequence email.

    CAN-SPAM (US) requires a valid physical postal address and a clear opt-out
    mechanism in every commercial email. The sender holds the outbox while
    postal_address is empty. The opt-out lines are also the quote markers the
    handler uses to cut our own text out of inbound replies, so keep them
    distinctive.
    """
    postal_address: str = ""
    opt_out_line_en: str = 'Not relevant? Reply "unsubscribe" and you won\'t hear from me again.'
    opt_out_line_es: str = '¿No es para ti? Responde "baja" y no te escribo más.'


DEMO_KINDS = ("voice", "website")
# Sequence steps an offer can give a call to action or angle for.
MAX_OFFER_STEP = 5


def _names(values: list[str]) -> list[str]:
    """Market and segment names compare case-insensitively."""
    return [v.strip().lower() for v in values or [] if v and v.strip()]


def _signal_codes(values: list[str]) -> list[str]:
    """Signal codes as the vocabulary stores them. Whether a code exists is
    a database question, checked by `mercury offers` and the Writer."""
    codes = []
    for v in values or []:
        code = (v or "").strip().upper()
        if not code:
            continue
        if not all(ch.isalnum() or ch == "_" for ch in code):
            raise ValueError(f"'{v}' is not a signal code (letters, digits and '_', e.g. NO_WEBSITE)")
        codes.append(code)
    return codes


class OfferSignalRule(BaseModel):
    """Which observed signals qualify a company for an offer: every code in
    ``require`` and none in ``exclude``, read the way a cohort reads them
    (the newest observation per signal, and only a positive one counts)."""
    require: list[str] = []
    exclude: list[str] = []

    @field_validator("require", "exclude")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        return _signal_codes(v)


class OfferContent(BaseModel):
    """The approved description of an offer. With a ``summary`` the brief is
    the only offer description the Writer sees for that prospect."""
    name: str = ""
    # What it is, in one or two plain sentences.
    summary: str = ""
    # The only things the email may claim about the offer.
    claims: list[str] = []


class OfferStep(BaseModel):
    """The call to action and angle of one sequence step."""
    cta: str = ""
    angle: str = ""


class CaseStudyScope(BaseModel):
    """Where a case study may be named. Empty lists mean anywhere."""
    markets: list[str] = []
    segments: list[str] = []

    @field_validator("markets", "segments")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return _names(v)


class CaseStudy(BaseModel):
    """A client story an offer may cite, in approved words, within a scope.

    ``name`` and ``aliases`` are what the pre-send gate looks for: a draft
    that names a case study outside its scope is blocked.
    """
    name: str
    aliases: list[str] = []
    summary: str = ""
    scope: CaseStudyScope = CaseStudyScope()

    @field_validator("name")
    @classmethod
    def _named(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("a case study needs a name")
        return v

    def terms(self) -> list[str]:
        return [t.strip() for t in [self.name, *self.aliases] if t and t.strip()]


class SupportingMaterial(BaseModel):
    """Optional metadata about something that supports an offer (a sample,
    a one-pager). Informational: nothing is held for lack of one."""
    name: str
    kind: str = ""
    url: str = ""
    notes: str = ""


class AggregateEvidence(BaseModel):
    """A statistic computed from Mercury's own observations, never typed in.

    Of the companies checked for every ``require`` signal (optionally only
    those in ``segments``), how many carry them all and none of
    ``exclude``. The brief quotes it only when at least ``min_sample``
    companies were checked.
    """
    description: str
    require: list[str]
    exclude: list[str] = []
    segments: list[str] = []
    min_sample: int = 20
    # Steps whose brief may quote it. Empty = every step.
    steps: list[int] = []

    @field_validator("require", "exclude")
    @classmethod
    def _codes(cls, v: list[str]) -> list[str]:
        return _signal_codes(v)

    @field_validator("segments")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return _names(v)

    @field_validator("min_sample")
    @classmethod
    def _positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("min_sample must be at least 1")
        return v

    @model_validator(mode="after")
    def _has_cohort(self):
        if not self.require:
            raise ValueError("evidence needs at least one signal under require")
        return self


class OfferDefinition(BaseModel):
    """One offer a prospect can be routed to (see mercury/offers.py).

    The key is what campaigns and outbox rows carry (``offer_key``). Routing
    takes the first offer, in configured order, whose rule matches the
    prospect: ``markets``, ``segments`` and ``signals``. An offer with no
    rule is only ever chosen as the ``default``, so a config written for the
    demo gate alone (key, requires_demo, demo_kind) routes nothing.
    """
    key: str
    # true: no sequence email of this offer leaves until a demo for that
    # prospect is marked ready (`mercury demos ready`).
    requires_demo: bool = False
    # What the demo is: voice (an answering line) or website (a draft site).
    demo_kind: str = ""
    # The fallback when no offer's rule matches. At most one.
    default: bool = False
    # Eligibility: icp.markets names, and segments (the prospect's or its
    # company's industry). Empty = any.
    markets: list[str] = []
    segments: list[str] = []
    signals: OfferSignalRule = OfferSignalRule()
    content: OfferContent = OfferContent()
    # Claim restrictions the brief states verbatim.
    restrictions: list[str] = []
    # Signal codes whose newest observation the brief gives as a verified
    # fact (SERP_RANK, ...). Empty = the codes under signals.require.
    facts: list[str] = []
    materials: list[SupportingMaterial] = []
    # Per step: {1: {cta, angle}, 2: ..., 3: ...}
    steps: dict[int, OfferStep] = {}
    case_studies: list[CaseStudy] = []
    evidence: AggregateEvidence | None = None

    @field_validator("key")
    @classmethod
    def _valid_key(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not v or not all(ch.isalnum() or ch in "_-" for ch in v):
            raise ValueError("offer key must be letters, digits, '-' or '_' (e.g. 'voice')")
        return v

    @field_validator("demo_kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v and v not in DEMO_KINDS:
            raise ValueError(f"demo_kind must be one of {', '.join(DEMO_KINDS)}")
        return v

    @field_validator("markets", "segments")
    @classmethod
    def _lower(cls, v: list[str]) -> list[str]:
        return _names(v)

    @field_validator("facts")
    @classmethod
    def _fact_codes(cls, v: list[str]) -> list[str]:
        return _signal_codes(v)

    @field_validator("steps")
    @classmethod
    def _valid_steps(cls, v: dict[int, OfferStep]) -> dict[int, OfferStep]:
        for step in v:
            if not 1 <= step <= MAX_OFFER_STEP:
                raise ValueError(f"steps are numbered 1 to {MAX_OFFER_STEP}, got {step}")
        return v

    @property
    def has_rule(self) -> bool:
        return bool(self.markets or self.segments or self.signals.require or self.signals.exclude)

    @property
    def label(self) -> str:
        return self.content.name.strip() or self.key

    def signal_codes(self) -> set[str]:
        """Every signal code this offer reads."""
        codes = set(self.signals.require) | set(self.signals.exclude) | set(self.facts)
        if self.evidence:
            codes |= set(self.evidence.require) | set(self.evidence.exclude)
        return codes


class DemosConfig(BaseModel):
    # A ready demo is retired this many days after the last email to a
    # prospect who never replied. The break-up email's "stays ready for N
    # days" must quote this number. 0 keeps demos until retired by hand.
    retire_after_days: int = 14

    @field_validator("retire_after_days")
    @classmethod
    def _non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("retire_after_days must be >= 0")
        return v


class MercuryConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    usage: UsageConfig = UsageConfig()
    compliance: ComplianceConfig = ComplianceConfig()
    offers: list[OfferDefinition] = []
    demos: DemosConfig = DemosConfig()

    @field_validator("offers")
    @classmethod
    def _unique_offers(cls, v: list[OfferDefinition]) -> list[OfferDefinition]:
        seen: set[str] = set()
        for offer in v:
            if offer.key in seen:
                raise ValueError(f"offer {offer.key} is listed twice")
            seen.add(offer.key)
        defaults = [o.key for o in v if o.default]
        if len(defaults) > 1:
            raise ValueError(f"only one offer can be the default, got {', '.join(defaults)}")
        return v

    @model_validator(mode="after")
    def _offer_markets_exist(self):
        """A market name that matches nothing would silently never route,
        so a typo fails here. Segments and signal codes are open sets."""
        known = {m.name.strip().lower() for m in self.icp.markets}
        for offer in self.offers:
            named = [("markets", m) for m in offer.markets] + [
                (f"case study {cs.name}", m) for cs in offer.case_studies for m in cs.scope.markets]
            for where, market in named:
                if market not in known:
                    have = ", ".join(sorted(known)) or "none (icp.markets is empty)"
                    raise ValueError(f"offer {offer.key}: {where} names market '{market}', "
                                     f"which is not in icp.markets (have: {have})")
        return self


class EnvConfig(BaseModel):
    instantly_api_key: str = ""
    # Discovery providers
    dataforseo_login: str = ""
    dataforseo_password: str = ""
    dataforseo_sandbox: str = ""   # any truthy value routes to the free sandbox
    linkedin_email: str = ""
    linkedin_password: str = ""
    hunter_api_key: str = ""
    serper_api_key: str = ""
    tavily_api_key: str = ""
    treg_token: str = ""          # treg.to: one prepaid balance for verification/enrichment
    semrush_api_key: str = ""
    reoon_api_key: str = ""
    zerobounce_api_key: str = ""
    # Native mail providers (channels.email.provider: gmail | smtp)
    gmail_client_id: str = ""
    gmail_client_secret: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    # Every MAILBOX_* variable, so channels.email.mailboxes[].password_env
    # can name any of them without a field per mailbox.
    mailbox_secrets: dict[str, str] = {}
    # Every PLACEMENT_* variable: app passwords of the placement test's seed
    # inboxes and control sender (channels.email.placement).
    placement_secrets: dict[str, str] = {}

    # Env vars a mailbox may take its password from besides MAILBOX_*. An
    # allowlist, so a typo such as password_env: TAVILY_API_KEY cannot hand
    # an API key to an SMTP server.
    _MAILBOX_PASSWORD_FIELDS = ("SMTP_PASSWORD", "IMAP_PASSWORD")

    def secret(self, name: str) -> str:
        """A mailbox password: a MAILBOX_* or PLACEMENT_* variable,
        SMTP_PASSWORD or IMAP_PASSWORD."""
        name = (name or "").strip()
        if name.startswith("MAILBOX_"):
            return self.mailbox_secrets.get(name, "")
        if name.startswith("PLACEMENT_"):
            return self.placement_secrets.get(name, "")
        if name in self._MAILBOX_PASSWORD_FIELDS:
            return getattr(self, name.lower(), "") or ""
        return ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> MercuryConfig:
    """Load Mercury configuration from YAML file.

    Raises ConfigError with a clear, actionable message on any problem.
    """
    if config_path is None:
        config_path = _find_config_file()

    try:
        with open(config_path) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigFileNotFoundError(
            f"Config file not found: {config_path}. "
            "Create one from mercury.yaml.example or run 'mercury setup'."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'mercury setup' to configure Mercury.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        return MercuryConfig(**data)
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'mercury setup'."
        )
        raise


def load_env(values=None) -> EnvConfig:
    """Load credentials: .env merged into the process environment, or the
    mapping ``values`` (read as-is, without touching os.environ)."""
    if values is None:
        load_dotenv(interpolate=False)
        values = os.environ
    getenv = lambda key, default="": (values.get(key) or default)  # noqa: E731
    env = EnvConfig(
        instantly_api_key=getenv("INSTANTLY_API_KEY", "").strip(),
        dataforseo_login=getenv("DATAFORSEO_LOGIN", "").strip(),
        dataforseo_password=getenv("DATAFORSEO_PASSWORD", "").strip(),
        dataforseo_sandbox=getenv("DATAFORSEO_SANDBOX", "").strip(),
        linkedin_email=getenv("LINKEDIN_EMAIL", "").strip(),
        linkedin_password=getenv("LINKEDIN_PASSWORD", "").strip(),
        hunter_api_key=getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=getenv("SERPER_API_KEY", "").strip(),
        tavily_api_key=getenv("TAVILY_API_KEY", "").strip(),
        treg_token=getenv("TREG_TOKEN", "").strip(),
        semrush_api_key=getenv("SEMRUSH_API_KEY", "").strip(),
        reoon_api_key=getenv("REOON_API_KEY", "").strip(),
        zerobounce_api_key=getenv("ZEROBOUNCE_API_KEY", "").strip(),
        gmail_client_id=getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=getenv("GMAIL_CLIENT_SECRET", "").strip(),
        smtp_host=getenv("SMTP_HOST", "").strip(),
        smtp_port=int(getenv("SMTP_PORT", "587").strip() or 587),
        smtp_username=getenv("SMTP_USERNAME", "").strip(),
        smtp_password=getenv("SMTP_PASSWORD", ""),
        imap_host=getenv("IMAP_HOST", "").strip(),
        imap_port=int(getenv("IMAP_PORT", "993").strip() or 993),
        imap_username=getenv("IMAP_USERNAME", "").strip(),
        imap_password=getenv("IMAP_PASSWORD", ""),
        mailbox_secrets={
            k: v for k, v in values.items() if k.startswith("MAILBOX_")
        },
        placement_secrets={
            k: v for k, v in values.items() if k.startswith("PLACEMENT_")
        },
    )
    return env


def _find_config_file() -> str:
    """Search for Mercury's config.

    ``mercury.local.yaml`` wins when present. It is gitignored, so a fork can
    carry a real product configuration (trained on an actual company) while
    the tracked ``mercury.yaml`` stays an untrained template — nobody
    publishes their positioning, pricing, and prospect targeting by accident.

    ``MERCURY_CONFIG`` names a config file explicitly and wins over both
    (scripts/seed_demo.py uses it to point a demo dashboard at a demo config).
    """
    explicit = os.environ.get("MERCURY_CONFIG", "").strip()
    if explicit:
        if Path(explicit).is_file():
            return explicit
        raise ConfigFileNotFoundError(f"MERCURY_CONFIG={explicit} does not exist.")
    candidates = [
        Path.cwd() / "mercury.local.yaml",
        PROJECT_ROOT / "mercury.local.yaml",
        Path.cwd() / "mercury.yaml",
        Path.cwd().parent / "mercury.yaml",
        PROJECT_ROOT / "mercury.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "mercury.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Create one from mercury.yaml.example or run 'mercury setup'."
    )
