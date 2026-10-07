"""Configuration loader for Mercury. Reads mercury.yaml + .env."""

import logging
import os
from datetime import date as _date
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator

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
    # Steady-state ceiling once warm-up has run its course.
    daily_cap: int = 30
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
    # With require_approval on, approving a first email also approves its
    # follow-ups (steps 2+), so a sequence you signed off on is not stuck
    # waiting for a second and third click. Replies still need approval.
    auto_approve_followups: bool = False
    # Pace the day's remaining sends evenly over the cycles left before
    # quiet hours, instead of sending up to MAX_SENDS_PER_CYCLE at once.
    spread_sends: bool = False
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

    @field_validator("warmup_initial_cap", "warmup_weekly_increase")
    @classmethod
    def _warmup_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("warm-up values must be >= 0")
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


class MercuryConfig(BaseModel):
    persona: PersonaConfig
    product: ProductConfig
    icp: ICPConfig
    channels: ChannelsConfig = ChannelsConfig()
    usage: UsageConfig = UsageConfig()
    compliance: ComplianceConfig = ComplianceConfig()


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

    # Env vars a mailbox may take its password from besides MAILBOX_*. An
    # allowlist, so a typo such as password_env: TAVILY_API_KEY cannot hand
    # an API key to an SMTP server.
    _MAILBOX_PASSWORD_FIELDS = ("SMTP_PASSWORD", "IMAP_PASSWORD")

    def secret(self, name: str) -> str:
        """A mailbox password: a MAILBOX_* variable, SMTP_PASSWORD or IMAP_PASSWORD."""
        name = (name or "").strip()
        if name.startswith("MAILBOX_"):
            return self.mailbox_secrets.get(name, "")
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
