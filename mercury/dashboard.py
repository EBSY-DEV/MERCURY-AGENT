"""Mercury Dashboard — local web UI to set up, control, and monitor Mercury."""

import asyncio
import json
import logging
import os
import re
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
import pathlib

import aiosqlite
import yaml
from dotenv import dotenv_values, load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import (
    HTMLResponse, JSONResponse, PlainTextResponse, Response,
)

logger = logging.getLogger("mercury.dashboard")

from mercury.paths import PROJECT_ROOT  # noqa: E402
from mercury.personas_api import router as personas_router  # noqa: E402
from mercury.imports_api import router as imports_router  # noqa: E402
from mercury.demos_api import router as demos_router  # noqa: E402
from mercury.control.audit import redact_text  # noqa: E402
from mercury.control.context import OperatorContext  # noqa: E402
from mercury.control.errors import (  # noqa: E402
    Conflict, ControlError, Forbidden, Invalid, NotFound, Unavailable,
)
from mercury.control.outbox import OutboxService, with_from_mailbox  # noqa: E402
from mercury.control.queries import (  # noqa: E402
    SIGNAL_CATEGORIES, SIGNAL_CATEGORY_ORDER, QueryService,
)
from mercury.exclusions_api import router as exclusions_router  # noqa: E402
# MERCURY_DB_PATH points the dashboard at another database (e.g. the demo
# DB from scripts/seed_demo.py) without touching the real one.
DB_PATH = Path(os.environ.get("MERCURY_DB_PATH") or (PROJECT_ROOT / "data" / "mercury.db"))
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "mercury.yaml"
PID_FILE = PROJECT_ROOT / "data" / "mercury.pid"
LOG_FILE = PROJECT_ROOT / "data" / "mercury.log"

app = FastAPI(title="Mercury Dashboard")
app.include_router(personas_router)
app.include_router(imports_router)
app.include_router(demos_router)
app.include_router(exclusions_router)

_env_lock = asyncio.Lock()
# Everything this server does, it does for the person at this machine.
DASHBOARD = OperatorContext.local("dashboard")
REQUEST_KEY_MAX = 200


def _ctx(request: Request | None = None) -> OperatorContext:
    """The local operator. A request that sends an ``Idempotency-Key``
    header gets it as its request key: a retry with the same key returns
    the first answer instead of running the command again."""
    key = (request.headers.get("Idempotency-Key") or "").strip() if request is not None else ""
    return OperatorContext.local("dashboard", request_id=key[:REQUEST_KEY_MAX]) if key else DASHBOARD


# ── Helpers ──


# Domain errors from mercury/control, as HTTP statuses. Request parsing and
# this translation stay here; the services never build a response.
CONTROL_STATUS = ((NotFound, 404), (Conflict, 409), (Forbidden, 403), (Invalid, 400))
CODE_STATUS = {"provider_failed": 502}


def _control_status(error: ControlError, overrides: dict | None = None) -> int:
    if overrides and error.code in overrides:
        return overrides[error.code]
    if error.code in CODE_STATUS:
        return CODE_STATUS[error.code]
    if isinstance(error, Unavailable):
        return 503
    return next((status for cls, status in CONTROL_STATUS if isinstance(error, cls)), 400)


def _control_error(error: ControlError, key: str = "message",
                   overrides: dict | None = None, detail: bool = False) -> JSONResponse:
    """The error as JSON. ``detail`` adds the stable code and its details;
    routes that predate the services leave it off to keep their old shape."""
    body = {"success": False, key: redact_text(str(error))}
    if detail:
        body |= {"code": error.code, **error.details}
    return JSONResponse(body, status_code=_control_status(error, overrides))


def _queries() -> QueryService:
    return QueryService(DASHBOARD, _state())


async def query_db(sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return results as list of dicts.

    Never raises: a missing DB file, missing table, or malformed schema
    returns [] so no dashboard route can 500 on an empty install.
    """
    if not DB_PATH.exists():
        return []
    try:
        async with aiosqlite.connect(str(DB_PATH)) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(sql, params) as cursor:
                rows = await cursor.fetchall()
                return [dict(r) for r in rows]
    except Exception as e:
        logger.warning("query_db failed (%s): %s", sql.split(None, 4)[:4], e)
        return []


def _mask_key(key: str) -> str:
    """Mask an API key for display: show first 4 and last 4 chars."""
    if not key or len(key) < 10:
        return "****" if key else ""
    return key[:4] + "****" + key[-4:]


def _read_env_file() -> dict[str, str]:
    """Read .env file and return as dict."""
    return {k: v for k, v in dotenv_values(ENV_FILE, interpolate=False).items()
            if v is not None} if ENV_FILE.exists() else {}


def _write_env_file(updates: dict[str, str]):
    """Update .env file with new values, preserving existing entries."""
    from mercury.inbox_settings import write_env

    write_env(ENV_FILE, updates)
    load_dotenv(str(ENV_FILE), override=True, interpolate=False)


def _runtime():
    from mercury.control.runtime import RuntimeService
    return RuntimeService(DASHBOARD, PROJECT_ROOT, PID_FILE, LOG_FILE)


def _check_mercury_pid() -> int | None:
    """Check if there's a running Mercury process from a PID file."""
    return _runtime().pid()


# ── Setup Status ──


@app.get("/api/setup-status")
async def get_setup_status():
    """Check what's configured and what still needs setup."""
    checks = []

    # 1. Venv
    checks.append({
        "id": "venv", "label": "Python virtual environment",
        "done": (PROJECT_ROOT / ".venv").is_dir(),
        "required": True,
        "help": "Run: python3 -m venv .venv && source .venv/bin/activate && pip install -e .",
    })

    # 2. Env file
    env_vars = _read_env_file()
    env_exists = ENV_FILE.exists() and bool(env_vars)
    checks.append({
        "id": "env_file", "label": "Environment file (.env)",
        "done": env_exists,
        "required": True,
        "help": "Go to the Settings tab to enter your API keys.",
    })

    # 3. Email provider configured (matches channels.email.provider)
    provider = _current_provider()

    def _has(*keys):
        return all((env_vars.get(k, "") or os.getenv(k, "")).strip() for k in keys)

    if provider == "gmail":
        provider_done = _has("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET") and \
            (PROJECT_ROOT / "data" / "gmail_token.json").is_file()
        provider_help = ("Set GMAIL_CLIENT_ID/SECRET in Settings, then run "
                         "'mercury gmail auth' in your terminal.")
    elif provider == "smtp":
        provider_done = _has("SMTP_HOST", "SMTP_USERNAME", "SMTP_PASSWORD")
        try:
            _config, pool = _mail_context()
            provider_done = bool(pool and pool.configured())
        except Exception:
            pass
        provider_help = "Add an inbox and its password in Settings → Sending inboxes."
    else:
        instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
        provider_done = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
        provider_help = "Enter your Instantly API key in Settings."
    checks.append({
        "id": "email_provider", "label": f"Email provider configured ({provider})",
        "done": provider_done,
        "required": True,
        "help": provider_help,
    })

    # 4. Email verification (needed for addresses to be sendable, not 'guess')
    verifier_set = _has("REOON_API_KEY") or _has("ZEROBOUNCE_API_KEY") or _has("HUNTER_API_KEY")
    checks.append({
        "id": "verifier", "label": "Email verification key",
        "done": verifier_set,
        "required": True,
        "help": "Add a Reoon (free 600/mo), ZeroBounce, or Hunter key in Settings — "
                "without one, found emails stay 'guess' and are never sent.",
    })

    # 5. Config valid
    config_valid = False
    try:
        from mercury.config import _find_config_file
        _cfg_path = pathlib.Path(_find_config_file())
    except Exception:
        _cfg_path = CONFIG_FILE
    if _cfg_path.exists():
        try:
            with open(_cfg_path) as f:
                cfg = yaml.safe_load(f)
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": "Mercury configured (mercury.yaml)",
        "done": config_valid,
        "required": True,
        "help": "Train Mercury on your product. Use the trainer or set up manually through Claude.",
    })

    # 6. Product trained
    product_trained = (PROJECT_ROOT / "skills" / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge trained",
        "done": product_trained,
        "required": True,
        "help": "Run: mercury train https://yourwebsite.com (or set up through Claude).",
    })

    # 7. LinkedIn (optional)
    linkedin_email = env_vars.get("LINKEDIN_EMAIL", "") or os.getenv("LINKEDIN_EMAIL", "")
    linkedin_pass = env_vars.get("LINKEDIN_PASSWORD", "") or os.getenv("LINKEDIN_PASSWORD", "")
    checks.append({
        "id": "linkedin", "label": "LinkedIn credentials",
        "done": bool(linkedin_email) and bool(linkedin_pass),
        "required": False,
        "help": "Optional. Enter your LinkedIn credentials in Settings to enable LinkedIn prospecting.",
    })

    # 8. Cloudflare (optional)
    cf_id = env_vars.get("CLOUDFLARE_ACCOUNT_ID", "") or os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
    cf_token = env_vars.get("CLOUDFLARE_API_TOKEN", "") or os.getenv("CLOUDFLARE_API_TOKEN", "")
    checks.append({
        "id": "cloudflare", "label": "Cloudflare deep crawling",
        "done": bool(cf_id) and bool(cf_token),
        "required": False,
        "help": "Optional. For JavaScript-rendered website crawling during training.",
    })

    required_checks = [c for c in checks if c["required"]]
    completed_required = sum(1 for c in required_checks if c["done"])

    return {
        "checks": checks,
        "completed": completed_required,
        "total_required": len(required_checks),
        "percent": int(completed_required / len(required_checks) * 100) if required_checks else 0,
    }


# ── Settings ──


def _current_provider() -> str:
    """Read channels.email.provider from the ACTIVE config (best-effort).

    Resolve it the way the rest of Mercury does: mercury.local.yaml wins when
    present. Reading the tracked template instead reports the wrong provider
    and declares a configured deployment unconfigured.
    """
    try:
        try:
            from mercury.config import _find_config_file
            cfg_path = _find_config_file()
        except Exception:
            cfg_path = CONFIG_FILE
        with open(cfg_path) as f:
            cfg = yaml.safe_load(f) or {}
        return ((cfg.get("channels") or {}).get("email") or {}).get("provider", "instantly")
    except Exception:
        return "instantly"


@app.get("/api/settings")
async def get_settings():
    """Get current settings — presence flags only for secrets, never raw values."""
    env_vars = _read_env_file()
    all_keys = [
        "INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
        "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
        "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
        "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
        "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
        "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
        "SERPER_API_KEY", "TAVILY_API_KEY", "SEMRUSH_API_KEY",
        "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "TREG_TOKEN",
    ]
    for key in all_keys:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    def is_set(k):
        return bool((env_vars.get(k) or "").strip())

    # Gmail is authorized once mercury gmail auth has stored a token file.
    gmail_token = (PROJECT_ROOT / "data" / "gmail_token.json").is_file()

    return {
        "provider": _current_provider(),
        # Non-secret values echo back so fields repopulate; secrets are
        # presence-only so keys never leave the box.
        "instantly_api_key_set": is_set("INSTANTLY_API_KEY"),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": is_set("LINKEDIN_PASSWORD"),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_set": is_set("CLOUDFLARE_API_TOKEN"),
        "gmail_client_id": env_vars.get("GMAIL_CLIENT_ID", ""),
        "gmail_client_secret_set": is_set("GMAIL_CLIENT_SECRET"),
        "gmail_authorized": gmail_token,
        "smtp_host": env_vars.get("SMTP_HOST", ""),
        "smtp_port": env_vars.get("SMTP_PORT", ""),
        "smtp_username": env_vars.get("SMTP_USERNAME", ""),
        "smtp_password_set": is_set("SMTP_PASSWORD"),
        "imap_host": env_vars.get("IMAP_HOST", ""),
        "imap_port": env_vars.get("IMAP_PORT", ""),
        "reoon_api_key_set": is_set("REOON_API_KEY"),
        "zerobounce_api_key_set": is_set("ZEROBOUNCE_API_KEY"),
        "hunter_api_key_set": is_set("HUNTER_API_KEY"),
        "serper_api_key_set": is_set("SERPER_API_KEY"),
        "tavily_api_key_set": is_set("TAVILY_API_KEY"),
        "semrush_api_key_set": is_set("SEMRUSH_API_KEY"),
        "treg_token_set": is_set("TREG_TOKEN"),
        "dataforseo_login": env_vars.get("DATAFORSEO_LOGIN", ""),
        "dataforseo_password_set": is_set("DATAFORSEO_PASSWORD"),
    }


@app.post("/api/settings/env")
async def save_env_settings(request: Request):
    """Save environment variables to .env file."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        updates = {}
        for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                     "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN",
                     "GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET",
                     "SMTP_HOST", "SMTP_PORT", "SMTP_USERNAME", "SMTP_PASSWORD",
                     "IMAP_HOST", "IMAP_PORT", "IMAP_USERNAME", "IMAP_PASSWORD",
                     "REOON_API_KEY", "ZEROBOUNCE_API_KEY", "HUNTER_API_KEY",
                     "SERPER_API_KEY", "TAVILY_API_KEY", "SEMRUSH_API_KEY",
                     "DATAFORSEO_LOGIN", "DATAFORSEO_PASSWORD", "TREG_TOKEN"]:
            if key in data and data[key] is not None:
                # Strip newlines so a crafted value can't inject extra .env entries
                value = str(data[key]).replace("\n", " ").replace("\r", " ")
                updates[key] = value if key in ("SMTP_PASSWORD", "IMAP_PASSWORD") else value.strip()
        if updates:
            try:
                _write_env_file(updates)
            except Exception as e:
                logger.warning("Failed to write .env: %s", e)
                return {"success": False, "message": "Could not write .env file."}
    return {"success": True}


@app.get("/api/config")
async def get_supported_config():
    """The mercury.yaml settings that can be changed from here, and their values."""
    from mercury.control.settings import ConfigService

    try:
        return await ConfigService(DASHBOARD, _state()).get()
    except ControlError as e:
        return _control_error(e, detail=True)
    except Exception as e:
        logger.warning("Could not read the config: %s", e)
        return JSONResponse({"success": False, "message": "Could not read the Mercury configuration."},
                            status_code=500)


@app.patch("/api/config")
async def update_supported_config(request: Request):
    """Change allowlisted settings: {"revision": <from GET>, "changes":
    {"dotted.path": value, ...}}. A secret or an unsupported field is refused
    before anything is written; so is a stale revision."""
    from mercury.control.settings import ConfigService

    body = await _json_body(request)
    if body is None:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        try:
            state = _state()
            await state.init_db()
            result = await ConfigService(_ctx(request), state).update(
                body.get("changes"), body.get("revision"))
        except ControlError as e:
            return _control_error(e, detail=True)
        except Exception as e:
            logger.warning("Could not save the config: %s", e)
            return JSONResponse({"success": False, "message": "Could not save the Mercury configuration."},
                                status_code=500)
    return {"success": True, **result, "restart_required": _check_mercury_pid() is not None}


@app.post("/api/settings/test-instantly")
async def test_instantly(request: Request):
    """Test an Instantly API key."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    api_key = str(data.get("api_key", "") or "")
    if not api_key:
        return {"success": False, "message": "No API key provided."}
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                "https://api.instantly.ai/api/v2/accounts",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if resp.status_code == 200:
                return {"success": True, "message": "Connected to Instantly."}
            else:
                return {"success": False, "message": f"API returned {resp.status_code}. Check your key."}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {str(e)}"}


# ── Companies ──


@app.get("/api/companies")
async def get_companies():
    """All companies with contact counts."""
    return await _queries().companies()


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    return await _queries().company_contacts(company_id)


# ── Feedback ──


@app.post("/api/feedback")
async def add_feedback(request: Request):
    """Add a comment/feedback on any entity."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    entity_type = str(data.get("entity_type", "") or "")[:50]
    entity_id = str(data.get("entity_id", "") or "")[:100]
    comment = str(data.get("comment", "") or "").strip()[:4000]
    if not comment:
        return {"success": False, "message": "Comment is required."}
    feedback_id = uuid.uuid4().hex[:12]
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        async with aiosqlite.connect(str(DB_PATH)) as db:
            # Ensure the table exists so feedback works even on a fresh install
            await db.execute(
                """CREATE TABLE IF NOT EXISTS feedback (
                    id TEXT PRIMARY KEY,
                    entity_type TEXT,
                    entity_id TEXT,
                    comment TEXT,
                    created_at TEXT DEFAULT (datetime('now'))
                )"""
            )
            await db.execute(
                "INSERT INTO feedback (id, entity_type, entity_id, comment) VALUES (?, ?, ?, ?)",
                (feedback_id, entity_type, entity_id, comment),
            )
            await db.commit()
    except Exception as e:
        logger.warning("Failed to save feedback: %s", e)
        return {"success": False, "message": "Could not save feedback."}
    return {"success": True, "id": feedback_id}


@app.get("/api/feedback/{entity_type}/{entity_id}")
async def get_feedback(entity_type: str, entity_id: str):
    """Get feedback for an entity."""
    rows = await query_db(
        "SELECT * FROM feedback WHERE entity_type = ? AND entity_id = ? ORDER BY created_at DESC",
        (entity_type, entity_id),
    )
    return rows


# ── Mercury Controls ──


@app.get("/api/mercury/status")
async def get_mercury_status():
    """Check if Mercury is currently running."""
    return await _runtime().status()


@app.post("/api/mercury/start")
async def start_mercury():
    """Start Mercury's heartbeat loop as a subprocess."""
    try:
        return {"success": True, **await _runtime().start()}
    except ControlError as e:
        return {"success": False, "message": str(e)}


@app.post("/api/mercury/stop")
async def stop_mercury(force: bool = False):
    """Ask Mercury to stop. It finishes the step it is on; ``stopping`` says
    it has not exited yet. ``?force=true`` kills it instead."""
    try:
        return {"success": True, **await _runtime().stop(force=force)}
    except ControlError as e:
        return {"success": False, "message": str(e)}


@app.get("/api/mercury/logs")
async def get_mercury_logs():
    """Get recent log lines."""
    return await _runtime().logs()


# ── Pipeline Data (existing endpoints) ──


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        return await _queries().stats()
    except Exception as e:
        return {"error": str(e)}


_USAGE_SUM = (
    "COUNT(DISTINCT CASE WHEN session_id != '' THEN session_id ELSE id END) AS calls, "
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cache_read_tokens), 0) AS cache_read_tokens, "
    "COALESCE(SUM(cache_creation_tokens), 0) AS cache_creation_tokens, "
    "ROUND(COALESCE(SUM(cost_usd), 0), 4) AS cost_usd"
)

_quota_client = None


@app.get("/api/usage")
async def get_usage():
    """Token/cost accounting + live subscription quota for the Usage tab."""
    global _quota_client

    totals = {}
    for label, where in (
        ("today", "date(created_at) = date('now')"),
        ("week", "created_at >= datetime('now', '-7 days')"),
        ("month", "created_at >= datetime('now', '-30 days')"),
    ):
        rows = await query_db(f"SELECT {_USAGE_SUM} FROM usage_events WHERE {where}")
        totals[label] = rows[0] if rows else {}

    def grouped(expr, alias):
        return (
            f"SELECT {expr} AS {alias}, {_USAGE_SUM} FROM usage_events "
            f"WHERE created_at >= datetime('now', '-30 days') "
            f"GROUP BY {alias} ORDER BY output_tokens DESC LIMIT 25"
        )

    by_agent = await query_db(grouped("CASE WHEN agent = '' THEN 'other' ELSE agent END", "agent"))
    by_task = await query_db(grouped("CASE WHEN task = '' THEN 'other' ELSE task END", "task"))
    by_model = await query_db(grouped("CASE WHEN model = '' THEN 'unknown' ELSE model END", "model"))
    by_day = await query_db(
        f"SELECT date(created_at) AS day, {_USAGE_SUM} FROM usage_events "
        f"WHERE created_at >= datetime('now', '-30 days') "
        f"GROUP BY day ORDER BY day ASC"
    )

    quota = None
    try:
        from mercury.integrations.quota import QuotaClient
        if _quota_client is None:
            _quota_client = QuotaClient()
        quota = await _quota_client.get_utilization()
    except Exception as e:
        logger.debug("Quota lookup failed: %s", e)

    return {
        "quota": quota,
        "totals": totals,
        "by_day": by_day,
        "by_agent": by_agent,
        "by_task": by_task,
        "by_model": by_model,
    }


@app.get("/api/prospects")
async def get_prospects():
    return await _queries().prospects()


def _state():
    from mercury.state import StateManager
    return StateManager(db_path=str(DB_PATH))


def _outbox(with_pool: bool = False, request: Request | None = None) -> OutboxService:
    """The outbox commands, wired the way the sender sees the world. The
    pool is only needed to say which mailbox an email goes out from."""
    pool = None
    if with_pool:
        try:
            _cfg, pool = _mail_context()
        except Exception:
            pool = None
    return OutboxService(_ctx(request), _state(), _demo_config(), pool)


@app.get("/api/outbox")
async def get_outbox_api():
    """Outbox queue + kill-switch state for the Outbox tab."""
    try:
        return await (await _outbox(with_pool=True).ready()).overview()
    except Exception as e:
        return {"error": str(e)}


def _demo_config():
    """The config the demo gate reads, or None when it can't be loaded. The
    gate fails closed on None: every email that carries an offer shows held."""
    try:
        from mercury.config import load_config

        return load_config()
    except Exception as e:
        logger.warning(f"Demo gate: could not load the config: {e}")
        return None


def _mail_context():
    """(config, pool) built exactly as the sender builds them. Re-reads .env
    on each call, so a password added by hand shows up without a restart."""
    from mercury.control.settings import mail_context

    return mail_context(ENV_FILE)


# Kept under its old name: the calendar and the tests use it.
_with_from_mailbox = with_from_mailbox


@app.get("/api/mailboxes")
async def get_mailboxes():
    """Sending capacity per mailbox, computed with the sender's own pool and
    rules: today's cap, warm-up stage, sends in the rolling 24 hours.
    Presence flags only; no secret leaves the box."""
    try:
        from mercury.integrations.mailboxes import mailbox_report

        config, pool = _mail_context()
        state = _state()
        await state.init_db()
        if pool is not None:
            # The same health gates the sender applies, so caps agree.
            from mercury.warmup import apply_health

            await apply_health(state, pool)
        return mailbox_report(config, pool, await state.count_outbox_sent_today_by_mailbox())
    except Exception as e:
        logger.error(f"/api/mailboxes: {e}")
        return {"error": f"Could not read the mail configuration: {type(e).__name__}. "
                         "Check mercury.local.yaml (channels.email) and the dashboard log."}


@app.get("/api/settings/mailboxes")
async def get_inbox_settings():
    from mercury.config import _find_config_file
    from mercury.inbox_settings import settings

    try:
        return settings(Path(_find_config_file()), ENV_FILE)
    except Exception:
        return JSONResponse({"error": "Could not read inbox settings. Check your Mercury configuration."},
                            status_code=400)


def _config_targets(found: str) -> tuple[Path, Path | None]:
    """(config read, private file written instead or None). Saving from the
    tracked template goes to mercury.local.yaml so real settings stay out of
    the repository."""
    source = Path(found)
    private = (source.with_name("mercury.local.yaml")
               if source.name == "mercury.yaml" and not os.getenv("MERCURY_CONFIG") else None)
    return source, private


@app.get("/api/settings/email-options")
async def get_email_options():
    from mercury.config import _find_config_file
    from mercury.inbox_settings import email_options

    try:
        return email_options(Path(_find_config_file()))
    except Exception:
        return JSONResponse({"error": "Could not read the sending settings. Check your Mercury configuration."},
                            status_code=400)


@app.post("/api/settings/email-options")
async def save_email_options_api(request: Request):
    """On/off sending switches under channels.email (thread_followups)."""
    from mercury.config import _find_config_file
    from mercury.inbox_settings import InboxError, save_email_options

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        try:
            source, private = _config_targets(_find_config_file())
            result = save_email_options(source, data, private)
            result["restart_required"] = _check_mercury_pid() is not None
            return result
        except InboxError as e:
            return JSONResponse({"success": False, "message": str(e)}, status_code=e.status)
        except Exception:
            logger.warning("Could not save sending settings", exc_info=False)
            return JSONResponse({"success": False, "message": "Could not save the setting. Check file permissions and configuration."},
                                status_code=500)


async def _save_inbox(request: Request, email: str | None = None):
    from mercury.control.settings import config_paths
    from mercury.inbox_settings import InboxError, save

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        try:
            source, private = config_paths()
            result = save(source, ENV_FILE, data, email, private)
            result["restart_required"] = _check_mercury_pid() is not None
            return result
        except InboxError as e:
            return JSONResponse({"success": False, "message": str(e)}, status_code=e.status)
        except Exception:
            logger.warning("Could not save inbox settings", exc_info=False)
            return JSONResponse({"success": False, "message": "Could not save inbox settings. Check file permissions and configuration."},
                                status_code=500)


@app.post("/api/settings/mailboxes")
async def add_inbox(request: Request):
    return await _save_inbox(request)


@app.patch("/api/settings/mailboxes/{email}")
async def edit_inbox(email: str, request: Request):
    return await _save_inbox(request, email)


@app.post("/api/settings/mailboxes/{email}/test")
async def test_inbox(email: str):
    from mercury.config import _find_config_file, load_config, load_env
    from mercury.inbox_settings import configured_inboxes, env_values
    from mercury.integrations.smtp_mail import SmtpImapProvider

    try:
        config = load_config(_find_config_file())
        env = load_env(env_values(ENV_FILE))
        box = next((b for b in configured_inboxes(config, env) if b.email == email.lower()), None)
        if box is None:
            return JSONResponse({"success": False, "message": "Inbox not found."}, status_code=404)
        ok, _message = await asyncio.wait_for(SmtpImapProvider(config, env, box).test_connection(), 35)
        # Provider errors may echo credential data; return only a generic result.
        return {"success": ok, "message": "SMTP and IMAP connected." if ok else
                "Could not connect. Check the inbox password, server settings, and SMTP/IMAP access."}
    except Exception:
        return {"success": False, "message": "Connection test failed or timed out. Check the inbox settings."}


@app.post("/api/outbox/approve-all")
async def outbox_approve_all(request: Request):
    """Approve the emails the reviewer was shown: {"items": [{"id", "revision"}]}.
    Anything not listed, or changed since, stays in review."""
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    try:
        result = await (await _outbox(request=request).ready()).approve_all(body.get("items"))
        return {"success": True, **result}
    except ControlError as e:
        return _control_error(e, detail=True)
    except Exception as e:
        return JSONResponse({"success": False, "message": redact_text(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str, request: Request):
    """Approve one email at the revision on screen: {"revision": n}."""
    body = await _json_body(request) or {}
    try:
        result = await (await _outbox(request=request).ready()).approve(item_id, body.get("revision"))
        return {"success": True, "followups_approved": result["followups_approved"],
                "revision": result["revision"]}
    except ControlError as e:
        if e.code in ("not_found", "not_pending"):
            return {"success": False, "followups_approved": 0}
        return _control_error(e, detail=True)
    except Exception as e:
        return JSONResponse({"success": False, "message": redact_text(e)}, status_code=500)


@app.post("/api/outbox/batch")
async def outbox_batch(request: Request):
    """Approve or reject a frozen list: {"action", "items": [{"id", "revision"}]}."""
    body = await _json_body(request)
    if body is None:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    try:
        result = await (await _outbox(request=request).ready()).batch(body.get("action"), body.get("items"))
        return {"success": True, **result}
    except ControlError as e:
        return _control_error(e, detail=True)
    except Exception as e:
        return JSONResponse({"success": False, "message": redact_text(e)}, status_code=500)


@app.get("/api/outbox/{item_id}")
async def outbox_item(item_id: str):
    """One email as the review desk shows it."""
    try:
        return await (await _outbox(with_pool=True).ready()).get(item_id)
    except ControlError as e:
        return _control_error(e, detail=True)


@app.post("/api/outbox/{item_id}/reject")
async def outbox_reject(item_id: str, request: Request):
    """Reject one queued email at the revision on screen: {"revision": n}."""
    body = await _json_body(request) or {}
    try:
        result = await (await _outbox(request=request).ready()).reject(item_id, body.get("revision"))
        return {"success": True, "rejected": result["rejected"]}
    except ControlError as e:
        if e.code in ("not_found", "not_queued"):
            return {"success": True, "rejected": 0}
        return _control_error(e, detail=True)
    except Exception as e:
        return JSONResponse({"success": False, "message": redact_text(e)}, status_code=500)


@app.put("/api/outbox/{item_id}")
async def outbox_edit(item_id: str, request: Request):
    """The reviewer edits a draft in place: {"subject", "body", "revision"}.
    The edit is a new revision; an approved draft goes back to review."""
    try:
        body = await request.json()
        subject = str(body.get("subject") or "").strip()[:200]
        text = str(body.get("body") or "").strip()[:4000]
        result = await (await _outbox(request=request).ready()).edit(
            item_id, subject, text, body.get("revision"))
        return {"success": True, "revision": result["revision"], "status": result["status"],
                "approval_cleared": result["approval_cleared"]}
    except NotFound:
        return JSONResponse({"success": False,
                             "message": "only pending or approved drafts can be edited"},
                            status_code=409)
    except ControlError as e:
        return _control_error(e, detail=True)
    except Exception as e:
        return JSONResponse({"success": False, "message": redact_text(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reschedule")
async def outbox_reschedule(item_id: str, request: Request):
    """Move a queued email to a new send time (stored as naive UTC):
    {"send_at", "revision"}. An approved email goes back to review."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body if isinstance(body, dict) else {}
    when = _parse_send_at(body.get("send_at"))
    if when is None:
        return JSONResponse(
            {"success": False, "error": "send_at must be an ISO datetime"},
            status_code=400)
    try:
        result = await (await _outbox(request=request).ready()).reschedule(
            item_id, when, body.get("revision"))
        return {"success": True, "send_at": result["send_at"], "revision": result["revision"],
                "status": result["status"], "approval_cleared": result["approval_cleared"]}
    except ControlError as e:
        return _control_error(e, "error", {"not_editable": 400}, detail=True)
    except Exception as e:
        return JSONResponse({"success": False, "error": redact_text(e)}, status_code=500)


# ── Out-of-office pauses ──
#
# A prospect who sent a vacation notice has their remaining cold sequence held
# back (see Handler._pause_for_ooo). The Outbox tab lists them, flags the ones
# whose return date needs a human, and lets an operator correct the date or
# resume them. Both actions are recorded in the activity log.


def _operator_clock():
    """(timezone name, quiet-hours end) from the config, or UTC defaults."""
    try:
        from mercury.config import load_config
        from mercury.ooo import operator_clock

        return operator_clock(load_config())
    except Exception:
        return "UTC", "07:00"


def _pause_view(row: dict, tz_name: str) -> dict:
    """One paused contact as the UI needs it, with the return date as the
    operator's calendar day (what the date field edits)."""
    resume_local = ""
    if row.get("resume_at"):
        try:
            import pytz

            when = datetime.fromisoformat(str(row["resume_at"]).replace(" ", "T"))
            resume_local = pytz.UTC.localize(when).astimezone(pytz.timezone(tz_name)).date().isoformat()
        except Exception:
            resume_local = ""
    name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
    return {
        "prospect_id": row["prospect_id"],
        "name": name or row.get("email") or "",
        "email": row.get("email") or "",
        "title": row.get("title") or "",
        "company": row.get("company_name") or "",
        "state": row["state"],
        "review_reason": row.get("review_reason") or "",
        "return_text": row.get("return_text") or "",
        "resume_at": row.get("resume_at") or "",
        "resume_local": resume_local,
        "confidence": row.get("confidence") or 0,
        "manual_override": bool(row.get("manual_override")),
        "queued_count": int(row.get("queued_count") or 0),
        "paused_since": row.get("created_at") or "",
        "heard_at": row.get("trigger_at") or "",
    }


@app.get("/api/pauses")
async def get_pauses():
    """Contacts whose cold sequence is paused for an out-of-office reply."""
    try:
        state = _state()
        await state.init_db()
        tz_name, _end = _operator_clock()
        rows = await state.list_pauses()
        return {"timezone": tz_name, "pauses": [_pause_view(r, tz_name) for r in rows]}
    except Exception as e:
        return {"error": str(e), "pauses": []}


@app.post("/api/pauses/{prospect_id}/resume")
async def resume_pause_api(prospect_id: str):
    """Resume a paused contact now. Their next unsent step goes out under the
    normal pacing; later steps keep their gaps."""
    try:
        state = _state()
        await state.init_db()
        before = await state.get_active_pause(prospect_id)
        if before is None:
            return JSONResponse(
                {"success": False, "error": "this contact has no active pause"}, status_code=404)
        pause = await state.resume_pause(prospect_id, reason="operator")
        if pause is None:
            return JSONResponse(
                {"success": False, "error": "this contact has no active pause"}, status_code=404)
        await state.log_action("sequence_resumed", "dashboard", {
            "prospect_id": prospect_id, "by": "operator",
            "was": before["state"], "resume_at": before.get("resume_at") or "",
            "rescheduled": pause.get("rescheduled", 0),
        })
        return {"success": True, "rescheduled": pause.get("rescheduled", 0)}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@app.post("/api/pauses/{prospect_id}/return-date")
async def set_pause_return_date(prospect_id: str, request: Request):
    """Correct a contact's return date (YYYY-MM-DD, the operator's calendar).
    Sending resumes at the first sending time on that day, weekends skipped.
    The date is kept: replaying an older message will not overwrite it."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = str((body or {}).get("return_date") or "").strip() if isinstance(body, dict) else ""
    day = _parse_day(raw)
    if day is None:
        return JSONResponse(
            {"success": False, "error": "return_date must be YYYY-MM-DD"}, status_code=400)
    try:
        from mercury.ooo import MAX_DAYS_AHEAD, resume_time

        tz_name, quiet_end = _operator_clock()
        import pytz

        today = datetime.now(pytz.timezone(tz_name)).date()
        if day < today:
            return JSONResponse(
                {"success": False,
                 "error": "that date has already passed; use Resume to continue now"},
                status_code=400)
        if (day - today).days > MAX_DAYS_AHEAD:
            return JSONResponse(
                {"success": False, "error": f"return date is more than {MAX_DAYS_AHEAD} days away"},
                status_code=400)
        state = _state()
        await state.init_db()
        before = await state.get_active_pause(prospect_id)
        if before is None:
            return JSONResponse(
                {"success": False, "error": "this contact has no active pause"}, status_code=404)
        when = resume_time(day, tz_name, quiet_end)
        pause = await state.override_pause(prospect_id, when)
        if pause is None:
            return JSONResponse(
                {"success": False, "error": "this contact has no active pause"}, status_code=404)
        await state.log_action("pause_date_changed", "dashboard", {
            "prospect_id": prospect_id, "by": "operator",
            "from": before.get("resume_at") or "", "was": before["state"],
            "to": pause.get("resume_at") or "", "now": pause["state"],
        })
        return {"success": True, "state": pause["state"], "resume_at": pause.get("resume_at") or ""}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ── Pipeline board + calendar ──

PIPELINE_COLUMNS = [
    ("new", "New", True, "Found and scored, no email drafted yet."),
    ("queued", "Queued", True, "Sequence drafted and waiting to send."),
    ("contacted", "Contacted", True, "First email sent, waiting for an answer."),
    ("replied", "Replied", False, "They wrote back — a conversation is open."),
    ("meeting", "Meeting", False, "A call is booked or being arranged."),
    ("won", "Won", False, "Deal closed. Follow-ups are stopped."),
    ("lost", "Lost", False, "Said no, opted out, or went cold."),
]
PIPELINE_CARD_CAP = 200
_PIPELINE_LOCKED = {key for key, _, locked, _ in PIPELINE_COLUMNS if locked}
_PIPELINE_KEYS = [key for key, *_ in PIPELINE_COLUMNS]

# Latest conversation per prospect, outbox rollups per prospect: two
# set-based CTEs joined onto prospects, so the board is one query.
_PIPELINE_SQL = """
WITH lc AS (
    SELECT prospect_id, id, stage, status, updated_at,
           ROW_NUMBER() OVER (
               PARTITION BY prospect_id
               ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC, rowid DESC
           ) AS rn
    FROM conversations
),
ob AS (
    SELECT prospect_id,
           SUM(CASE WHEN status = 'sent' THEN 1 ELSE 0 END) AS sent_count,
           SUM(CASE WHEN status IN ('pending_review', 'approved') THEN 1 ELSE 0 END)
               AS pending_count,
           MIN(CASE WHEN status IN ('pending_review', 'approved') THEN send_at END)
               AS next_send_at,
           MAX(CASE WHEN status = 'sent' THEN sent_at END) AS last_sent_at
    FROM outbox
    WHERE prospect_id != ''
    GROUP BY prospect_id
)
SELECT p.id, p.first_name, p.last_name, p.title, p.email, p.email_status,
       p.score, p.status, p.updated_at,
       COALESCE(NULLIF(c.name, ''), p.company, '') AS company_name,
       lc.id AS conversation_id, lc.stage AS stage,
       lc.updated_at AS convo_updated_at,
       COALESCE(ob.sent_count, 0) AS sent_count,
       COALESCE(ob.pending_count, 0) AS pending_count,
       ob.next_send_at, ob.last_sent_at
FROM prospects p
LEFT JOIN companies c ON c.id = p.company_id
LEFT JOIN lc ON lc.prospect_id = p.id AND lc.rn = 1
LEFT JOIN ob ON ob.prospect_id = p.id
"""


def _ts_key(value) -> str:
    """Sortable form of a stored timestamp ('T' or ' ' separated)."""
    return str(value).replace("T", " ") if value else ""


def _parse_send_at(raw) -> datetime | None:
    """Parse 'YYYY-MM-DDTHH:MM' or full ISO (optional Z/offset) to naive UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    if when.tzinfo is not None:
        when = when.astimezone(timezone.utc).replace(tzinfo=None)
    return when.replace(microsecond=0)


def _pipeline_column(status: str, stage: str, has_convo: bool) -> str:
    """Board column for a prospect. First match wins."""
    if status in ("lost", "opted_out") or stage == "closed_lost":
        return "lost"
    if status == "closed" or stage == "closed_won":
        return "won"
    if status == "meeting":
        return "meeting"
    if status == "replied" or has_convo:
        return "replied"
    if status == "contacted":
        return "contacted"
    if status == "queued":
        return "queued"
    return "new"


def _pipeline_card(row: dict) -> dict:
    name = f"{row.get('first_name') or ''} {row.get('last_name') or ''}".strip()
    stamps = [s for s in (row.get("updated_at"), row.get("convo_updated_at"),
                          row.get("last_sent_at")) if s]
    last_activity = max(stamps, key=_ts_key) if stamps else None
    return {
        "id": row["id"],
        "name": name or (row.get("email") or ""),
        "title": row.get("title") or "",
        "company": row.get("company_name") or "",
        "email": row.get("email") or "",
        "email_status": row.get("email_status") or "",
        "score": row.get("score"),
        "status": row.get("status") or "new",
        "stage": row.get("stage") or "",
        "conversation_id": row.get("conversation_id") or "",
        "next_send_at": row.get("next_send_at"),
        "last_activity": str(last_activity) if last_activity else None,
        "sent_count": int(row.get("sent_count") or 0),
        "pending_count": int(row.get("pending_count") or 0),
    }


async def _latest_conversation(prospect_id: str) -> dict | None:
    rows = await query_db(
        "SELECT id, stage, status FROM conversations WHERE prospect_id = ? "
        "ORDER BY datetime(updated_at) DESC, datetime(created_at) DESC, rowid DESC "
        "LIMIT 1", (prospect_id,))
    return rows[0] if rows else None


@app.get("/api/pipeline")
async def get_pipeline():
    """The deal board: every prospect in exactly one of seven columns."""
    try:
        await _state().init_db()
    except Exception as e:
        logger.debug("pipeline init_db failed: %s", e)
    rows = await query_db(_PIPELINE_SQL)
    buckets: dict[str, list[dict]] = {key: [] for key in _PIPELINE_KEYS}
    for row in rows:
        col = _pipeline_column(row.get("status") or "", row.get("stage") or "",
                               bool(row.get("conversation_id")))
        buckets[col].append(_pipeline_card(row))

    def _score(card):
        try:
            return float(card["score"]) if card["score"] is not None else float("-inf")
        except (TypeError, ValueError):
            return float("-inf")

    columns = []
    for key, label, locked, hint in PIPELINE_COLUMNS:
        cards = buckets[key]
        cards.sort(key=lambda c: (_ts_key(c["last_activity"]), _score(c)), reverse=True)
        columns.append({
            "key": key, "label": label, "locked": locked, "hint": hint,
            "count": len(cards), "items": cards[:PIPELINE_CARD_CAP],
        })
    return {"columns": columns}


@app.post("/api/pipeline/{prospect_id}/move")
async def move_pipeline_card(prospect_id: str, request: Request):
    """Drag a card to a human-owned column (replied / meeting / won / lost)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    column = (body or {}).get("column") if isinstance(body, dict) else None
    if column not in _PIPELINE_KEYS:
        return JSONResponse(
            {"success": False, "error": f"unknown column: {column!r}"}, status_code=400)
    if column in _PIPELINE_LOCKED:
        return JSONResponse(
            {"success": False,
             "error": f"'{column}' is set by Mercury automatically and can't be chosen by hand"},
            status_code=400)
    try:
        state = _state()
        await state.init_db()
        prospect = await state.get_prospect(prospect_id)
        if not prospect:
            return JSONResponse(
                {"success": False, "error": "prospect not found"}, status_code=404)
        convo = await _latest_conversation(prospect_id)
        stage = (convo or {}).get("stage") or ""
        closed_stage = stage in ("closed_won", "closed_lost")
        cancelled = 0

        if column == "replied":
            await state.update_prospect_status(prospect_id, "replied")
            if convo and closed_stage:
                await state.update_conversation(convo["id"], stage="engaged", status="open")
        elif column == "meeting":
            await state.update_prospect_status(prospect_id, "meeting")
            if convo:
                # A closed stage would out-rank 'meeting' on the board, so a
                # card dragged back from Won/Lost reopens at 'closing'.
                updates = {"stage": "closing"}
                if closed_stage:
                    updates["status"] = "open"
                await state.update_conversation(convo["id"], **updates)
        elif column == "won":
            await state.update_prospect_status(prospect_id, "closed")
            if convo:
                await state.update_conversation(convo["id"], stage="closed_won", status="closed")
        elif column == "lost":
            await state.update_prospect_status(prospect_id, "lost")
            if convo:
                await state.update_conversation(convo["id"], stage="closed_lost", status="closed")

        if column in ("meeting", "won", "lost"):
            cancelled = await state.cancel_pending_outbox_for_prospect(
                prospect_id, reason=f"moved_to_{column}")

        try:
            await state.log_action("pipeline_move", "dashboard", {
                "prospect_id": prospect_id, "from_status": prospect.status,
                "column": column, "cancelled": cancelled,
            })
        except Exception as e:
            logger.debug("pipeline move log_action failed: %s", e)
        return {"success": True, "column": column, "cancelled": cancelled}
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


_CAL_EVENT_EXPR = (
    "CASE WHEN o.status = 'sent' AND o.sent_at IS NOT NULL "
    "THEN o.sent_at ELSE o.send_at END"
)
CALENDAR_MAX_SPAN_DAYS = 62


def _parse_day(value: str | None) -> date | None:
    if not value or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _outbox_label(kind: str, step) -> str:
    if kind == "reply":
        return "Reply"
    try:
        n = int(step or 1)
    except (TypeError, ValueError):
        n = 1
    return "Email 1" if n <= 1 else f"Follow-up {n - 1}"


@app.get("/api/calendar")
async def get_calendar(start: str | None = None, end: str | None = None):
    """Every outbox email whose send (or scheduled send) falls in [start, end)."""
    d0, d1 = _parse_day(start), _parse_day(end)
    if d0 is None or d1 is None:
        return JSONResponse(
            {"error": "start and end are required as YYYY-MM-DD"}, status_code=400)
    if d1 <= d0:
        return JSONResponse({"error": "end must be after start"}, status_code=400)
    if (d1 - d0).days > CALENDAR_MAX_SPAN_DAYS:
        return JSONResponse(
            {"error": f"range is limited to {CALENDAR_MAX_SPAN_DAYS} days"}, status_code=400)
    try:
        await _state().init_db()
    except Exception as e:
        logger.debug("calendar init_db failed: %s", e)
    rows = await query_db(
        f"""SELECT o.id, {_CAL_EVENT_EXPR} AS at, o.kind, o.step, o.status, o.revision,
                   o.subject, o.to_email, o.prospect_id, o.campaign_id, o.error,
                   o.body, COALESCE(o.mailbox, '') AS mailbox, p.first_name, p.last_name,
                   COALESCE(NULLIF(c.name, ''), p.company, '') AS company_name
            FROM outbox o
            LEFT JOIN prospects p ON p.id = o.prospect_id
            LEFT JOIN companies c ON c.id = p.company_id
            WHERE datetime({_CAL_EVENT_EXPR}) >= datetime(?)
              AND datetime({_CAL_EVENT_EXPR}) < datetime(?)
            ORDER BY datetime({_CAL_EVENT_EXPR}) ASC, o.step ASC""",
        (d0.isoformat(), d1.isoformat()),
    )
    # The address each email goes (or went) out from, resolved like the
    # Outbox does: '' = a new thread that has not rotated onto a mailbox yet.
    legacy_email = ""
    try:
        _cfg, pool = _mail_context()
        legacy_email = pool.legacy.email if pool else ""
    except Exception:
        pass
    try:
        rows = await _with_from_mailbox(_state(), rows, legacy_email)
    except Exception as e:
        logger.debug("calendar mailbox resolve failed: %s", e)
    items = []
    for r in rows:
        name = f"{r.get('first_name') or ''} {r.get('last_name') or ''}".strip()
        kind = r.get("kind") or "sequence"
        items.append({
            "id": r["id"],
            "at": r["at"],
            "kind": kind,
            "step": int(r.get("step") or 1),
            "label": _outbox_label(kind, r.get("step")),
            "status": r.get("status") or "",
            "revision": int(r.get("revision") or 1),
            "subject": r.get("subject") or "",
            "to_email": r.get("to_email") or "",
            "prospect_id": r.get("prospect_id") or "",
            "name": name or (r.get("to_email") or ""),
            "company": r.get("company_name") or "",
            "campaign_id": r.get("campaign_id") or "",
            "error": r.get("error") or "",
            "body": r.get("body") or "",
            "mailbox": r.get("from_mailbox", r.get("mailbox")) or "",
        })
    return {"start": start, "end": end, "items": items}


@app.post("/api/outbox/{item_id}/regenerate")
async def outbox_regenerate(item_id: str, request: Request):
    """Ask the Writer for a new draft of this email, optionally with an instruction."""
    try:
        try:
            body = await request.json()
        except Exception:
            body = {}
        body = body if isinstance(body, dict) else {}
        instruction = str(body.get("instruction") or "").strip()[:500]
        updated = await (await _outbox(request=request).ready()).regenerate(
            item_id, instruction, body.get("revision"))
        return {"success": True, **updated}
    except NotFound as e:
        if e.code != "not_found":
            return _control_error(e)
        return JSONResponse({"success": False,
                             "message": "only pending or approved drafts can be regenerated"},
                            status_code=409)
    except ControlError as e:
        return _control_error(e, detail=True)
    except Exception as e:
        return JSONResponse({"success": False, "message": redact_text(e)}, status_code=500)


def _sending():
    from mercury.control.sending import SendingService

    return SendingService(DASHBOARD, _state(), _demo_config())


@app.get("/api/sending/status")
async def sending_status():
    """The operator pause, every hold and what is in flight: the same read
    `mercury sending status` prints."""
    try:
        return await (await _sending().ready()).status()
    except ControlError as e:
        return _control_error(e)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/sending/{action}")
async def sending_toggle(action: str):
    """pause / resume (the operator pause only) / clear-hold (the bounce kill
    switch). Each answers with the status after it."""
    if action not in ("pause", "resume", "clear-hold"):
        return JSONResponse({"success": False, "message": "unknown action"}, status_code=400)
    try:
        service = await _sending().ready()
        run = {"pause": service.pause, "resume": service.resume,
               "clear-hold": service.clear_hold}[action]
        return {"success": True, **await run()}
    except ControlError as e:
        return _control_error(e)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/export/prospects.csv")
async def export_prospects(all: bool = False, min_score: int = 0, email_status: str = ""):
    """Sequencer-ready CSV download of the prospect list."""
    from mercury.state import StateManager
    from mercury.export import export_prospects_csv

    state = StateManager(db_path=str(DB_PATH))
    try:
        await state.init_db()
        statuses = [s.strip() for s in email_status.split(",") if s.strip()] or None
        _, text = await export_prospects_csv(
            state, email_statuses=statuses, min_score=min_score, include_all=all,
        )
    except Exception as e:
        logger.warning("Prospect export failed: %s", e)
        text = ""
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="prospects.csv"'},
    )


@app.get("/api/campaigns")
async def get_campaigns():
    return await _queries().campaigns()


@app.get("/api/conversations")
async def get_conversations():
    return await _queries().conversations()


@app.get("/api/activity")
async def get_activity():
    return await _queries().activity()


# ── Dashboard UI ──


# ── Discovery: the provider menu, an estimate, and a run ──
#
# This is the only stage that spends money, so the UI never starts one without
# showing what it will cost first.

def _discovery(config=None):
    from mercury.control.discovery import DiscoveryService
    return DiscoveryService(DASHBOARD, _state(), config)


def _discovery_args(body: dict) -> dict:
    cities = [c.strip() for c in (body.get("cities") or []) if c.strip()] or None
    return {"provider": body.get("provider") or "", "cities": cities,
            "depth": int(body.get("depth") or 30), "limit": int(body.get("limit") or 100)}


@app.get("/api/discover/providers")
async def get_discovery_providers():
    """What each source does, what it costs, and whether it's ready to use."""
    try:
        return await (await _discovery().ready()).providers()
    except Exception as e:
        logger.exception("discovery providers failed")
        return {"providers": [], "error": str(e)}


@app.post("/api/discover/estimate")
async def estimate_discovery(request: Request):
    """Projected spend and the exact query list, before anything is called."""
    try:
        body = await request.json()
        return await _discovery().estimate(**_discovery_args(body))
    except Invalid as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/discover/run")
async def start_discovery(request: Request):
    """Kick off a run in the background and hand back immediately.

    Discovery takes minutes, not milliseconds — holding the request open
    would just time out. Progress shows up in the run log.
    """
    from mercury.control import discovery

    if discovery.running():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        body = await request.json()
        args = _discovery_args(body)
        max_spend = float(body.get("max_spend") or 1.0)
        service = await _discovery().ready()
        return {"success": True, **await service.submit(**args, max_spend=max_spend)}
    except ControlError as e:
        return _control_error(e)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/profile/run")
async def start_profile():
    """Read the websites of everything discovered but not yet looked at.

    Free and model-free, so there is nothing to estimate and no cap to set.
    """
    try:
        return {"success": True, **await (await _discovery().ready()).submit_profile()}
    except ControlError as e:
        return _control_error(e)
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/discover/stop")
async def stop_discovery():
    """Kill switch. Read between batches, so an in-flight run stops cleanly."""
    try:
        await (await _discovery().ready()).stop()
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.get("/api/today")
async def get_today():
    """What needs a human, right now.

    The dashboard opens here rather than on a setup checklist: the question a
    returning user actually has is "is anything waiting on me?", and the
    answer is usually a short list or nothing at all.
    """
    items: list[dict] = []
    stats: dict = {}
    try:
        from mercury.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        proposed = [c for c in codes if c.get("status") == "proposed"]
        confirmed = [c for c in codes if c.get("status") == "confirmed"]
        pending = await state.get_outbox(status="pending_review", limit=200)
        approved = await state.get_outbox(status="approved", limit=200)
        sending = await _sending().status()
        counts = await state.count_prospects_by_status()

        convos = await query_db(
            "SELECT COUNT(*) AS n FROM conversations WHERE status = 'open'"
        )
        open_convos = convos[0]["n"] if convos else 0
        companies = await query_db("SELECT COUNT(*) AS n FROM companies")
        n_companies = companies[0]["n"] if companies else 0

        unprofiled_count = await state.count_companies_needing_profile()
        stats = {
            "companies": n_companies,
            "prospects": sum(counts.values()),
            "signals_confirmed": len(confirmed),
            "outbox_pending": len(pending),
            "outbox_approved": len(approved),
            "open_conversations": open_convos,
            "unprofiled": unprofiled_count,
        }

        # Ordered by how much it blocks Mercury from doing anything at all.
        # A health hold first: resuming does not lift it.
        for hold in sending["holds"]:
            if hold["scope"] != "global":
                continue
            items.append({
                "key": "hold:" + hold["kind"], "tone": "bad",
                "title": "Sending is on hold",
                "detail": hold["reason"].rstrip(".") + ". Resuming does not lift this hold.",
                "action": "Review the hold", "tab": "outbox",
            })
        if sending["paused"]:
            items.append({
                "key": "paused", "tone": "bad",
                "title": "Sending is paused",
                "detail": sending["reason"].rstrip(".") + ". Nothing new will go out until you resume it.",
                "action": "Review and resume", "tab": "outbox",
            })

        setup = await get_setup_status()
        if isinstance(setup, dict) and setup.get("percent", 100) < 100:
            missing = [c["label"] for c in setup.get("checks", [])
                       if c.get("required") and not c.get("done")]
            items.append({
                "key": "setup", "tone": "warn",
                "title": "Finish setting Mercury up",
                "detail": ", ".join(missing[:3]) or "Some required steps are incomplete.",
                "action": "Open setup", "tab": "settings",
            })

        if proposed:
            items.append({
                "key": "signals", "tone": "warn",
                "title": (f"{len(proposed)} signal waiting for your confirmation"
                          if len(proposed) == 1
                          else f"{len(proposed)} signals waiting for your confirmation"),
                "detail": ("Mercury won't collect anything you haven't approved. "
                           "Confirm which signals define a good prospect for you."),
                "action": "Review signals", "tab": "signals",
            })
        elif not confirmed:
            items.append({
                "key": "signals-none", "tone": "warn",
                "title": "No signals confirmed",
                "detail": "Every signal is rejected, so prospecting has nothing to collect.",
                "action": "Review signals", "tab": "signals",
            })

        if not n_companies and confirmed:
            items.append({
                "key": "discover", "tone": "good",
                "title": "No companies yet",
                "detail": ("Signals are confirmed but nothing has been collected. "
                           "Discovery is free to try — no account needed."),
                "action": "Find businesses", "tab": "discover",
            })

        unprofiled = unprofiled_count
        if unprofiled:
            items.append({
                "key": "profile", "tone": "good",
                "title": f"{unprofiled} companies not looked at yet",
                "detail": ("Reading their websites is free and it is what makes "
                           "an email specific — who their agency is, what they "
                           "are missing, whether they are spending on ads."),
                "action": "Read their sites", "tab": "discover",
            })

        try:
            health = await _health_report(state)
            cancel = [d for d in health.get("domains", [])
                      if d.get("verdict") == "CANCEL_CANDIDATE"]
        except Exception as e:
            logger.debug("today: health report failed: %s", e)
            cancel = []
        if cancel:
            items.append({
                "key": "deliverability", "tone": "warn",
                "title": (f"{cancel[0]['domain']} is a candidate to cancel" if len(cancel) == 1
                          else f"{len(cancel)} sending domains are candidates to cancel"),
                "detail": ((cancel[0]["reason"] + " " if len(cancel) == 1 else "")
                           + "Run a placement test (mercury mail placement) to tell the "
                             "domain apart from the copy before you retire it."),
                "action": "Open mailboxes", "tab": "mailboxes",
            })

        if pending:
            items.append({
                "key": "outbox", "tone": "warn",
                "title": (f"{len(pending)} email waiting for approval" if len(pending) == 1
                          else f"{len(pending)} emails waiting for approval"),
                "detail": "Nothing sends until you approve it. Read them one at a time.",
                "action": "Open the decisions desk", "tab": "outbox",
            })

        review = (await state.count_active_pauses()).get("needs_review", 0)
        if review:
            items.append({
                "key": "pauses", "tone": "warn",
                "title": (f"{review} contact is out of office with no usable return date"
                          if review == 1 else
                          f"{review} contacts are out of office with no usable return date"),
                "detail": ("Their sequences are paused and stay paused until you set a "
                           "return date or resume them."),
                "action": "Review paused contacts", "tab": "outbox",
            })

        from mercury.demos import waiting_for_demo

        waiting_demo = await waiting_for_demo(state, _demo_config())
        stats["waiting_demo"] = len(waiting_demo)
        if waiting_demo:
            n = len(waiting_demo)
            items.append({
                "key": "demos", "tone": "warn",
                "title": (f"{n} contact waiting for a demo" if n == 1
                          else f"{n} contacts waiting for a demo"),
                "detail": ("Their emails say something was already built for them, so "
                           "they wait until you mark the demo ready."),
                "action": "See who is waiting", "tab": "outbox",
            })

        blocked = await state.get_outbox(status="blocked", limit=200)
        if blocked:
            items.append({
                "key": "blocked", "tone": "warn",
                "title": (f"{len(blocked)} email blocked by an exclusion" if len(blocked) == 1
                          else f"{len(blocked)} emails blocked by exclusions"),
                "detail": ("They were queued before the address or domain was excluded. "
                           "Nothing goes out unless you lift the rule and approve again."),
                "action": "Review exclusions", "tab": "exclusions",
            })

        holds = await state.list_company_holds()
        held_mail = sum(h["queued"] for h in holds)
        if holds:
            items.append({
                "key": "company-holds", "tone": "good",
                "title": (f"{len(holds)} company on hold" if len(holds) == 1
                          else f"{len(holds)} companies on hold"),
                "detail": ("Someone there replied or you paused it, so cold mail to their "
                           f"colleagues waits ({held_mail} queued). Replies still go out."),
                "action": "Review holds", "tab": "exclusions",
            })

        if open_convos:
            items.append({
                "key": "replies", "tone": "good",
                "title": (f"{open_convos} live conversation" if open_convos == 1
                          else f"{open_convos} live conversations"),
                "detail": "People replied. Check how Mercury is handling them.",
                "action": "Read conversations", "tab": "conversations",
            })

        return {"items": items, "stats": stats}
    except Exception as e:
        logger.exception("today load failed")
        return {"items": [], "stats": stats, "error": str(e)}


# ── Signals: Mercury proposes, the user confirms ──
#
# Nothing is collected until a human has said yes to it. This is the gate the
# whole prospecting pipeline hangs off: collectors ask `state.confirmed_signal_codes()`
# and skip anything that isn't in the set.

CATEGORY_META = SIGNAL_CATEGORIES
CATEGORY_ORDER = SIGNAL_CATEGORY_ORDER


@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, grouped for review, with live cohort sizes."""
    try:
        return await (await _queries().ready()).signals()
    except Exception as e:
        logger.exception("signals load failed")
        return {"error": str(e), "groups": [], "summary": {}}


@app.post("/api/signals/status")
async def set_signals_status(request: Request):
    """Confirm or reject one signal, or a whole category at once."""
    try:
        body = await request.json()
        status = (body.get("status") or "").strip()
        codes = body.get("codes") or ([body["code"]] if body.get("code") else [])
        if status not in ("proposed", "confirmed", "rejected"):
            return JSONResponse(
                {"success": False, "message": f"invalid status: {status!r}"},
                status_code=400,
            )
        if not codes:
            return JSONResponse(
                {"success": False, "message": "no signal codes given"}, status_code=400
            )

        from mercury.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seed first: a confirm that arrives before anything has loaded the
        # catalog would otherwise report success while changing nothing.
        await seed_signal_catalog(state)

        changed, unknown = 0, []
        for code in codes:
            if await state.set_signal_status(code, status):
                changed += 1
            else:
                unknown.append(code)
        return {
            "success": True, "changed": changed,
            "status": status, "unknown": unknown,
        }
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/cohort")
async def preview_cohort(request: Request):
    """How many companies carry ALL these signals and none of those.

    The payoff for confirming signals: a cohort is a query, not a list.
    """
    try:
        body = await request.json()
        require = [c for c in (body.get("require") or []) if c]
        if not require:
            return {"size": 0, "companies": []}
        return await (await _queries().ready()).cohort(require, body.get("exclude") or [])
    except Exception as e:
        return JSONResponse({"size": 0, "companies": [], "error": str(e)}, status_code=500)


@app.get("/api/runs")
async def get_runs_api():
    """The collector run log — what ran, when, what it produced and cost."""
    try:
        return await (await _queries().ready()).runs()
    except Exception as e:
        return {"error": str(e)}


# ── Trends ──

TREND_WINDOWS = (7, 30, 90)


@app.get("/api/trends")
async def get_trends(days: str = "30"):
    """Daily sent / replies / positive / bounces, plus totals and the prior
    window. Definitions live in mercury/metrics.py."""
    try:
        n = int(days)
    except (TypeError, ValueError):
        n = 0
    if n not in TREND_WINDOWS:
        return JSONResponse(
            {"success": False, "error": "days must be one of 7, 30, 90"}, status_code=400)
    from mercury import metrics
    try:
        await _state().init_db()
    except Exception as e:
        logger.debug("trends init_db failed: %s", e)
    return await metrics.trends(str(DB_PATH), n, datetime.now(timezone.utc).date())


@app.get("/api/heatmap")
async def get_heatmap(weeks: str = "53"):
    """Outreach sends per day for the GitHub-style activity grid on Today."""
    try:
        n = int(weeks)
    except (TypeError, ValueError):
        n = 0
    if not 1 <= n <= 53:
        return JSONResponse({"success": False, "error": "weeks must be 1-53"}, status_code=400)
    from mercury import metrics
    try:
        await _state().init_db()
    except Exception as e:
        logger.debug("heatmap init_db failed: %s", e)
    return await metrics.heatmap(str(DB_PATH), n, datetime.now(timezone.utc).date())


async def _health_report(state) -> dict:
    """Per-domain deliverability verdicts plus the last placement test.
    Definitions and thresholds live in mercury/deliverability.py."""
    from mercury import deliverability, placement

    try:
        config, pool = _mail_context()
    except Exception as e:
        logger.debug("health: mail config unreadable: %s", e)
        from mercury.config import load_config
        config, pool = load_config(), None
    report = await deliverability.domain_report(state, config, pool)
    report["placement"] = await placement.report(state)
    report["thresholds_text"] = deliverability.thresholds_text(report)
    return report


@app.get("/api/health")
async def get_health():
    """The ``mercury health`` numbers for the Today card and the Mailboxes tab."""
    try:
        state = _state()
        await state.init_db()
        return await _health_report(state)
    except Exception as e:
        logger.error(f"/api/health: {e}", exc_info=True)
        return _err(f"Could not build the health report: {type(e).__name__}", 500)


# ── Inbox warm-up ──
#
# Which inboxes exist, their caps and start dates come from mercury.yaml
# (channels.email.mailboxes) — these endpoints never write config. What they
# do write is the overlay in warmup_inboxes: pause/resume, the checklist and
# notes. See mercury/warmup.py.


def _err(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"success": False, "error": message}, status_code=status)


async def _json_body(request: Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


async def _warmup_mailbox(email: str):
    """(state, pool, mailbox) for an address in the mail config, or a
    JSONResponse explaining why it can't be edited."""
    try:
        _config, pool = _mail_context()
    except Exception as e:
        logger.error(f"warm-up: mail config unreadable: {e}")
        return _err(f"Could not read the mail configuration: {type(e).__name__}", 500)
    if pool is None:
        return _err("warm-up applies to the gmail and smtp providers only")
    key = (email or "").strip().lower()
    mailbox = next((mb for mb in pool.mailboxes if mb.email and mb.email == key), None)
    if mailbox is None:
        return _err("that inbox is not in channels.email.mailboxes", 404)
    state = _state()
    await state.init_db()
    return state, pool, mailbox


@app.get("/api/warmup")
async def get_warmup():
    from mercury import warmup
    try:
        config, pool = _mail_context()
    except Exception as e:
        logger.error(f"/api/warmup: {e}")
        return {"error": f"Could not read the mail configuration: {type(e).__name__}. "
                         "Check mercury.local.yaml (channels.email) and the dashboard log.",
                "inboxes": [], "config_hint": warmup.CONFIG_HINT}
    try:
        state = _state()
        await state.init_db()
        return await warmup.overview(state, config, pool)
    except Exception as e:
        logger.error(f"/api/warmup: {e}", exc_info=True)
        return _err(str(e), 500)


@app.get("/api/warmup/dns")
async def get_warmup_dns(domain: str | None = None):
    from mercury import warmup
    if domain is None or not domain.strip():
        try:
            _config, pool = _mail_context()
            domain = pool.primary.domain if pool else None
        except Exception:
            domain = None
    normalized = warmup.normalize_domain(domain)
    if not normalized:
        return _err("a valid domain is required (or configure a sending email)")
    result = await warmup.check_dns(normalized)
    try:
        state = _state()
        await state.init_db()
        await state.set_setting(warmup.dns_setting_key(normalized), json.dumps(result))
    except Exception as e:
        logger.debug("dns result persist failed: %s", e)
    return result


WARMUP_ACTIONS = ("pause", "resume")


@app.post("/api/warmup/inboxes/{email}/action")
async def warmup_inbox_action(email: str, request: Request):
    from mercury import warmup
    body = await _json_body(request)
    action = (body or {}).get("action")
    if action not in WARMUP_ACTIONS:
        return _err(f"action must be one of {', '.join(WARMUP_ACTIONS)}")
    ctx = await _warmup_mailbox(email)
    if isinstance(ctx, JSONResponse):
        return ctx
    state, _pool, mailbox = ctx
    row = await state.get_warmup_inbox(mailbox.email) or {}
    paused = row.get("status") == "paused"
    if action == "pause":
        if paused:
            return _err("inbox is already paused")
        await warmup.set_paused(state, mailbox.email, "paused manually")
    else:
        if not paused:
            return _err("inbox is not paused")
        await warmup.set_resumed(state, mailbox.email)
    await state.log_action("warmup_" + action, "dashboard", {"email": mailbox.email})
    return {"success": True}


@app.post("/api/warmup/inboxes/{email}/task")
async def warmup_inbox_task(email: str, request: Request):
    from mercury import warmup
    body = await _json_body(request)
    if body is None:
        return _err("invalid request body")
    key = body.get("key")
    if key in warmup.AUTO_TASKS:
        return _err("that task is checked automatically from DNS")
    if key not in warmup.TASK_KEYS:
        return _err("unknown task")
    if not isinstance(body.get("done"), bool):
        return _err("done must be true or false")
    ctx = await _warmup_mailbox(email)
    if isinstance(ctx, JSONResponse):
        return ctx
    state, _pool, mailbox = ctx
    await warmup.set_task(state, mailbox.email, key, body["done"])
    return {"success": True}


@app.post("/api/warmup/inboxes/{email}")
async def update_warmup_inbox(email: str, request: Request):
    """Notes only. Caps and start dates live in mercury.yaml."""
    from mercury import warmup
    body = await _json_body(request)
    if body is None:
        return _err("invalid request body")
    if "target_daily" in body or "start_date" in body:
        return _err("daily caps and start dates come from mercury.yaml "
                    "(channels.email.mailboxes); edit them there")
    if not isinstance(body.get("notes"), str):
        return _err("nothing to update (notes)")
    ctx = await _warmup_mailbox(email)
    if isinstance(ctx, JSONResponse):
        return ctx
    state, _pool, mailbox = ctx
    await warmup.set_notes(state, mailbox.email, body["notes"][:4000])
    return {"success": True}


WEB_DIR = (Path(__file__).resolve().parent / "web")


TEXT_TYPES = {".css": "text/css", ".js": "text/javascript", ".svg": "image/svg+xml",
              ".webmanifest": "application/manifest+json"}
BINARY_TYPES = {".woff2": "font/woff2", ".woff": "font/woff", ".png": "image/png"}


@app.get("/static/{path:path}")
async def static_file(path: str):
    """Serve the dashboard's own assets from disk.

    Read per-request rather than cached at import: editing app.css and hitting
    reload is the whole point of having them as real files. Fonts are vendored
    rather than fetched from a CDN — this is a local tool and it should work
    with the network off.
    """
    target = (WEB_DIR / path).resolve()
    root = WEB_DIR.resolve()
    if not target.is_file() or not target.is_relative_to(root):
        return PlainTextResponse("not found", status_code=404)

    if target.suffix in BINARY_TYPES:
        return Response(
            target.read_bytes(),
            media_type=BINARY_TYPES[target.suffix],
            headers={"Cache-Control": "public, max-age=604800"},
        )
    return PlainTextResponse(
        target.read_text(),
        media_type=TEXT_TYPES.get(target.suffix, "text/plain"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return (WEB_DIR / "index.html").read_text()


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server."""
    import uvicorn

    print(f"\n  Mercury Dashboard running at http://{host}:{port}")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
