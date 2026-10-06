"""Configuration loader for Harvey. Reads harvey.yaml + .env."""

import logging
import os
from datetime import date as _date
from datetime import time as _time
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError, field_validator

from harvey.paths import PROJECT_ROOT

logger = logging.getLogger("harvey.config")


class ConfigError(Exception):
    """Raised when Harvey's configuration is missing or invalid."""


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
    # Steady-state ceiling once warm-up has run its course.
    daily_cap: int = 30
    # First day this mailbox sent cold mail. The cap starts at
    # channels.email.warmup_initial_cap and grows weekly from here. Leave
    # empty for a mailbox that is already warm. A date in the future means
    # "not yet": the mailbox sends nothing until then.
    warmup_start: _date | None = None
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
    # outbox for your approval (dashboard → Outbox, or `harvey outbox`).
    # Set false for full autopilot once you trust the output.
    require_approval: bool = True
    # Kill switch: pause all sending when bounces exceed this fraction of
    # sent mail (measured over the trailing sends). 0 disables the switch.
    max_bounce_rate: float = 0.05
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

    @field_validator("max_daily_sends")
    @classmethod
    def _sends_non_negative(cls, v: int) -> int:
        if v < 0:
            raise ValueError("max_daily_sends must be >= 0")
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


class HarveyConfig(BaseModel):
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

    def secret(self, name: str) -> str:
        """Value of the env var ``name``: a MAILBOX_* entry or a known field."""
        name = (name or "").strip()
        if not name:
            return ""
        if name in self.mailbox_secrets:
            return self.mailbox_secrets[name]
        value = getattr(self, name.lower(), "")
        return value if isinstance(value, str) else ""


def _format_validation_error(e: ValidationError) -> str:
    """Turn a pydantic ValidationError into a readable, actionable message."""
    lines = []
    for err in e.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "(root)"
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(config_path: str | None = None) -> HarveyConfig:
    """Load Harvey configuration from YAML file.

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
            "Create one from harvey.yaml.example or run 'harvey setup'."
        )
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {config_path}:\n  {e}")
    except OSError as e:
        raise ConfigError(f"Could not read {config_path}: {e}")

    if data is None:
        raise ConfigError(f"{config_path} is empty. Run 'harvey setup' to configure Harvey.")
    if not isinstance(data, dict):
        raise ConfigError(
            f"{config_path} must contain a YAML mapping (key: value pairs), "
            f"got {type(data).__name__}."
        )

    try:
        return HarveyConfig(**data)
    except ValidationError as e:
        # Log a friendly, actionable summary, then re-raise the original
        # ValidationError so callers (and tests) keep the pydantic type.
        logger.error(
            f"Invalid configuration in {config_path}:\n{_format_validation_error(e)}\n"
            "Fix the fields above or re-run 'harvey setup'."
        )
        raise


def load_env() -> EnvConfig:
    """Load environment variables from .env file."""
    load_dotenv()
    env = EnvConfig(
        instantly_api_key=os.getenv("INSTANTLY_API_KEY", "").strip(),
        dataforseo_login=os.getenv("DATAFORSEO_LOGIN", "").strip(),
        dataforseo_password=os.getenv("DATAFORSEO_PASSWORD", "").strip(),
        dataforseo_sandbox=os.getenv("DATAFORSEO_SANDBOX", "").strip(),
        linkedin_email=os.getenv("LINKEDIN_EMAIL", "").strip(),
        linkedin_password=os.getenv("LINKEDIN_PASSWORD", "").strip(),
        hunter_api_key=os.getenv("HUNTER_API_KEY", "").strip(),
        serper_api_key=os.getenv("SERPER_API_KEY", "").strip(),
        tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip(),
        treg_token=os.getenv("TREG_TOKEN", "").strip(),
        semrush_api_key=os.getenv("SEMRUSH_API_KEY", "").strip(),
        reoon_api_key=os.getenv("REOON_API_KEY", "").strip(),
        zerobounce_api_key=os.getenv("ZEROBOUNCE_API_KEY", "").strip(),
        gmail_client_id=os.getenv("GMAIL_CLIENT_ID", "").strip(),
        gmail_client_secret=os.getenv("GMAIL_CLIENT_SECRET", "").strip(),
        smtp_host=os.getenv("SMTP_HOST", "").strip(),
        smtp_port=int(os.getenv("SMTP_PORT", "587").strip() or 587),
        smtp_username=os.getenv("SMTP_USERNAME", "").strip(),
        smtp_password=os.getenv("SMTP_PASSWORD", "").strip(),
        imap_host=os.getenv("IMAP_HOST", "").strip(),
        imap_port=int(os.getenv("IMAP_PORT", "993").strip() or 993),
        imap_username=os.getenv("IMAP_USERNAME", "").strip(),
        imap_password=os.getenv("IMAP_PASSWORD", "").strip(),
        mailbox_secrets={
            k: v.strip() for k, v in os.environ.items() if k.startswith("MAILBOX_")
        },
    )
    return env


def _find_config_file() -> str:
    """Search for Harvey's config.

    ``harvey.local.yaml`` wins when present. It is gitignored, so a fork can
    carry a real product configuration (trained on an actual company) while
    the tracked ``harvey.yaml`` stays an untrained template — nobody
    publishes their positioning, pricing, and prospect targeting by accident.
    """
    candidates = [
        Path.cwd() / "harvey.local.yaml",
        PROJECT_ROOT / "harvey.local.yaml",
        Path.cwd() / "harvey.yaml",
        Path.cwd().parent / "harvey.yaml",
        PROJECT_ROOT / "harvey.yaml",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    raise ConfigFileNotFoundError(
        "harvey.yaml not found in "
        + ", ".join(str(p.parent) for p in candidates)
        + ". Create one from harvey.yaml.example or run 'harvey setup'."
    )
