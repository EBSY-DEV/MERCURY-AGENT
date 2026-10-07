"""Mercury Dashboard — local web UI to set up, control, and monitor Mercury."""

import asyncio
import json
import logging
import os
import re
import signal
import subprocess
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
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

# Mercury process tracking
_mercury_process: subprocess.Popen | None = None
_mercury_started_at: datetime | None = None
_env_lock = asyncio.Lock()


# ── Helpers ──


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


def _check_mercury_pid() -> int | None:
    """Check if there's a running Mercury process from a PID file."""
    global _mercury_process, _mercury_started_at
    if _mercury_process and _mercury_process.poll() is None:
        return _mercury_process.pid
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            return pid
        except (ValueError, ProcessLookupError, PermissionError):
            PID_FILE.unlink(missing_ok=True)
    return None


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
    rows = await query_db("""
        SELECT c.*,
            (SELECT COUNT(*) FROM prospects p WHERE p.company_id = c.id) as contact_count
        FROM companies c ORDER BY c.created_at DESC LIMIT 200
    """)
    return rows


@app.get("/api/companies/{company_id}/contacts")
async def get_company_contacts(company_id: str):
    """Get all contacts for a specific company."""
    rows = await query_db(
        "SELECT * FROM prospects WHERE company_id = ? ORDER BY score DESC",
        (company_id,),
    )
    return rows


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
    pid = _check_mercury_pid()
    started = _mercury_started_at.isoformat() if _mercury_started_at else None
    return {"running": pid is not None, "pid": pid, "started_at": started}


@app.post("/api/mercury/start")
async def start_mercury():
    """Start Mercury's heartbeat loop as a subprocess."""
    global _mercury_process, _mercury_started_at

    if _check_mercury_pid():
        return {"success": False, "message": "Mercury is already running."}

    # Ensure data dir exists
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    try:
        log_handle = open(LOG_FILE, "a")
        try:
            _mercury_process = subprocess.Popen(
                [sys.executable, "-m", "mercury", "run"],
                cwd=str(PROJECT_ROOT),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start Mercury: %s", e)
        return {"success": False, "message": f"Failed to start Mercury: {e}"}
    _mercury_started_at = datetime.now()

    # Write PID file
    try:
        PID_FILE.write_text(str(_mercury_process.pid))
    except OSError as e:
        logger.warning("Could not write PID file: %s", e)

    return {"success": True, "pid": _mercury_process.pid}


@app.post("/api/mercury/stop")
async def stop_mercury():
    """Stop the Mercury subprocess."""
    global _mercury_process, _mercury_started_at

    pid = _check_mercury_pid()
    if not pid:
        return {"success": False, "message": "Mercury is not running."}

    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for graceful shutdown
        for _ in range(10):
            try:
                os.kill(pid, 0)
                await asyncio.sleep(0.5)
            except ProcessLookupError:
                break
        else:
            # Force kill if still running
            try:
                os.kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
    except (ProcessLookupError, PermissionError):
        pass

    _mercury_process = None
    _mercury_started_at = None
    PID_FILE.unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/mercury/logs")
async def get_mercury_logs():
    """Get recent log lines."""
    if not LOG_FILE.exists():
        return {"lines": []}
    try:
        # Tail only the last 64KB so a huge log file never blocks the UI
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", errors="replace")
        lines = text.strip().splitlines()[-100:]
        return {"lines": lines}
    except Exception:
        return {"lines": []}


# ── Pipeline Data (existing endpoints) ──


@app.get("/api/stats")
async def get_stats():
    """Pipeline overview stats."""
    try:
        prospects = await query_db(
            "SELECT status, COUNT(*) as count FROM prospects GROUP BY status"
        )
        prospect_total = sum(r["count"] for r in prospects)
        prospect_map = {r["status"]: r["count"] for r in prospects}

        campaigns = await query_db(
            "SELECT status, COUNT(*) as count FROM campaigns GROUP BY status"
        )
        campaign_map = {r["status"]: r["count"] for r in campaigns}

        conversations = await query_db(
            "SELECT status, COUNT(*) as count FROM conversations GROUP BY status"
        )
        convo_map = {r["status"]: r["count"] for r in conversations}

        actions = await query_db("SELECT COUNT(*) as count FROM actions")
        action_count = actions[0]["count"] if actions else 0

        usage = await query_db(
            "SELECT claude_calls FROM usage_log WHERE date = date('now')"
        )
        usage_today = usage[0]["claude_calls"] if usage else 0

        return {
            "prospects": {"total": prospect_total, "by_status": prospect_map},
            "campaigns": {"total": sum(campaign_map.values()), "by_status": campaign_map},
            "conversations": {"total": sum(convo_map.values()), "by_status": convo_map},
            "actions_total": action_count,
            "claude_calls_today": usage_today,
        }
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
    rows = await query_db("SELECT * FROM prospects ORDER BY created_at DESC LIMIT 200")
    return rows


def _state():
    from mercury.state import StateManager
    return StateManager(db_path=str(DB_PATH))


@app.get("/api/outbox")
async def get_outbox_api():
    """Outbox queue + kill-switch state for the Outbox tab."""
    try:
        state = _state()
        await state.init_db()
        try:
            _cfg, pool = _mail_context()
            legacy = pool.legacy.email if pool else ""
            known = {mb.email for mb in pool.mailboxes} if pool else None
        except Exception:
            legacy, known = "", None
        return {
            "paused": await state.get_setting("sending_paused"),
            "pending": await _with_from_mailbox(
                state, await state.get_outbox(status="pending_review", limit=100), legacy, known),
            "approved": await _with_from_mailbox(
                state, await state.get_outbox(status="approved", limit=50), legacy, known),
            "sending": await _with_from_mailbox(
                state, await state.get_outbox(status="sending", limit=50), legacy, known),
            "sent": await _with_from_mailbox(state, await query_db(
                "SELECT * FROM outbox WHERE status = 'sent' "
                "ORDER BY sent_at DESC LIMIT 25"), legacy),
            "failed": (await query_db(
                "SELECT * FROM outbox WHERE status IN ('failed','rejected','cancelled') "
                "ORDER BY updated_at DESC LIMIT 25")),
        }
    except Exception as e:
        return {"error": str(e)}


def _mail_context():
    """(config, pool) built exactly as the sender builds them. Re-reads .env
    on each call, so a password added by hand shows up without a restart."""
    from mercury.config import load_config, load_env
    from mercury.integrations.mailboxes import MailboxPool

    from dotenv import dotenv_values

    # Read .env over a copy of the environment; never mutate os.environ
    # here (the agent the dashboard starts inherits it).
    values = dict(os.environ)
    if ENV_FILE.exists():
        values.update({k: v for k, v in dotenv_values(str(ENV_FILE), interpolate=False).items() if v is not None})
    config = load_config()
    return config, MailboxPool.from_config(config, load_env(values))


async def _with_from_mailbox(state, rows: list[dict], legacy_email: str = "",
                             known: set[str] | None = None) -> list[dict]:
    """Add ``from_mailbox``: the address an email goes (or went) out from,
    resolved the way the sender resolves it. A follow-up inherits its
    opener's mailbox, '' on an old thread means the legacy mailbox, and a
    new thread whose opener has not gone out yet stays '' (it rotates)."""
    need = [r.get("campaign_id") or "" for r in rows
            if not r.get("mailbox") and r.get("kind") == "sequence"
            and int(r.get("step") or 1) > 1]
    threads = await state.get_thread_mailboxes(need)
    for r in rows:
        fm = r.get("mailbox") or ""
        if not fm:
            if r.get("status") == "sent" or r.get("kind") == "reply":
                fm = legacy_email
            elif r.get("kind") == "sequence" and int(r.get("step") or 1) > 1:
                key = (r.get("campaign_id") or "", r.get("prospect_id") or "")
                if key in threads:
                    fm = threads[key] or legacy_email
        r["from_mailbox"] = fm
        # Queued mail pinned to a mailbox no longer configured is held by
        # the sender (never re-routed); say so in the UI.
        r["from_removed"] = bool(fm and known is not None and fm not in known
                                 and r.get("status") != "sent")
    from mercury.personas import PersonaStore
    return await PersonaStore(state).enrich(rows)


async def _promote_followups_if_enabled(state, item: dict | None = None) -> int:
    """auto_approve_followups: promote right away on approval, so the
    follow-ups leave the review desk instead of waiting for the next cycle."""
    try:
        from mercury.config import load_config

        if not getattr(load_config().channels.email, "auto_approve_followups", False):
            return 0
    except Exception:
        return 0
    thread = {}
    if item and item.get("campaign_id"):
        thread = {"campaign_id": item["campaign_id"], "prospect_id": item.get("prospect_id") or ""}
    total = 0
    for _ in range(10):
        n = await state.approve_ready_followups(**thread)
        if not n:
            break
        total += n
    return total


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


async def _save_inbox(request: Request, email: str | None = None):
    from mercury.config import _find_config_file
    from mercury.inbox_settings import InboxError, save

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"success": False, "message": "Invalid request body."}, status_code=400)
    async with _env_lock:
        try:
            source = Path(_find_config_file())
            private = (source.with_name("mercury.local.yaml")
                       if source.name == "mercury.yaml" and not os.getenv("MERCURY_CONFIG") else None)
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
async def outbox_approve_all():
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox()
        await _promote_followups_if_enabled(state)
        return {"success": True, "approved": n}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/approve")
async def outbox_approve(item_id: str):
    try:
        state = _state()
        await state.init_db()
        n = await state.approve_outbox(item_id)
        followups = 0
        if n:
            followups = await _promote_followups_if_enabled(
                state, await state.get_outbox_item(item_id))
        return {"success": bool(n), "followups_approved": followups}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reject")
async def outbox_reject(item_id: str):
    try:
        state = _state()
        await state.init_db()
        n = await state.reject_outbox_item(item_id)
        return {"success": True, "rejected": n}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.put("/api/outbox/{item_id}")
async def outbox_edit(item_id: str, request: Request):
    """The reviewer edits a draft in place. Approved mail stays approved."""
    try:
        body = await request.json()
        subject = str(body.get("subject") or "").strip()[:200]
        text = str(body.get("body") or "").strip()[:4000]
        if not subject or not text:
            return JSONResponse({"success": False, "message": "subject and body are required"},
                                status_code=400)
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item or item.get("status") not in ("pending_review", "approved"):
            return JSONResponse({"success": False,
                                 "message": "only pending or approved drafts can be edited"},
                                status_code=409)
        if not await state.edit_outbox_item(item_id, subject=subject, body=text, manually_edited=1):
            return JSONResponse({"success": False, "message": "this draft has started sending"},
                                status_code=409)
        return {"success": True}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/outbox/{item_id}/reschedule")
async def outbox_reschedule(item_id: str, request: Request):
    """Move a queued email to a new send time (stored as naive UTC)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw = (body or {}).get("send_at") if isinstance(body, dict) else None
    when = _parse_send_at(raw)
    if when is None:
        return JSONResponse(
            {"success": False, "error": "send_at must be an ISO datetime"},
            status_code=400)
    try:
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item:
            return JSONResponse(
                {"success": False, "error": "outbox item not found"}, status_code=404)
        if item.get("status") not in ("pending_review", "approved"):
            return JSONResponse(
                {"success": False,
                 "error": f"cannot reschedule an email that is {item.get('status')}"},
                status_code=400)
        if when < _utc_naive_now() - timedelta(minutes=1):
            return JSONResponse(
                {"success": False, "error": "send_at is in the past"}, status_code=400)
        normalized = when.isoformat(timespec="seconds")
        if not await state.edit_outbox_item(item_id, send_at=normalized):
            return JSONResponse({"success": False, "error": "this draft has started sending"},
                                status_code=409)
        try:
            await state.log_action("outbox_reschedule", "dashboard", {
                "outbox_id": item_id, "from": item.get("send_at"), "to": normalized,
            })
        except Exception as e:
            logger.debug("reschedule log_action failed: %s", e)
        return {"success": True, "send_at": normalized}
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


def _utc_naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


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
        f"""SELECT o.id, {_CAL_EVENT_EXPR} AS at, o.kind, o.step, o.status,
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
        instruction = str((body or {}).get("instruction") or "").strip()[:500]
        state = _state()
        await state.init_db()
        item = await state.get_outbox_item(item_id)
        if not item or item.get("status") not in ("pending_review", "approved"):
            return JSONResponse({"success": False,
                                 "message": "only pending or approved drafts can be regenerated"},
                                status_code=409)
        prospect = await state.get_prospect(item["prospect_id"])
        if not prospect:
            return JSONResponse({"success": False, "message": "prospect not found"}, status_code=404)
        from mercury.agents.writer import Writer
        from mercury.brain import Brain
        from mercury.config import load_config, load_env

        from mercury.personas import PersonaStore
        if item.get("kind") == "reply":
            from mercury.agents.handler import Handler
            convo = await state.get_conversation(item.get("conversation_id") or "")
            if not convo:
                return JSONResponse({"success": False, "message": "conversation not found"}, status_code=404)
            config = load_config()
            handler = Handler(Brain(state), state, config, load_env())
            profile = await PersonaStore(state).for_generation(config, item.get("generation_id", ""))
            latest = next((m.content for m in reversed(convo.thread) if not m.is_ours), "")
            response = await handler._generate_response(
                convo.intent, latest, prospect, convo, instruction=instruction, profile=profile,
            )
            draft = {"subject": item["subject"], "body": response,
                     "generation_id": handler._response_generation_id} if response else None
        else:
            writer = Writer(Brain(state), state, load_config(), load_env())
            draft = await writer.regenerate_email(item, prospect, instruction)
        if not draft:
            return JSONResponse({"success": False, "message": "the writer returned nothing; try again"},
                                status_code=502)
        # A regenerated draft is unread: back to the review queue.
        try:
            await PersonaStore(state).replace_draft(item_id, draft)
        except ValueError as error:
            return JSONResponse({"success": False, "message": str(error)}, status_code=409)
        updated = await PersonaStore(state).enrich([await state.get_outbox_item(item_id)])
        return {"success": True, **updated[0]}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/sending/{action}")
async def sending_toggle(action: str):
    if action not in ("pause", "resume"):
        return JSONResponse({"success": False, "message": "unknown action"}, status_code=400)
    try:
        state = _state()
        await state.init_db()
        if action == "pause":
            await state.set_setting("sending_paused", "paused from dashboard")
        else:
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
        return {"success": True}
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
    rows = await query_db("SELECT * FROM campaigns ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["sequence"] = json.loads(row.get("sequence_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["sequence"] = []
        try:
            row["prospect_ids"] = json.loads(row.get("prospect_ids_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["prospect_ids"] = []
    return rows


@app.get("/api/conversations")
async def get_conversations():
    rows = await query_db("""
        SELECT c.*, p.first_name, p.last_name, p.email as prospect_email, p.company
        FROM conversations c
        LEFT JOIN prospects p ON c.prospect_id = p.id
        ORDER BY c.updated_at DESC LIMIT 100
    """)
    for row in rows:
        try:
            row["thread"] = json.loads(row.get("thread_json", "[]"))
        except (json.JSONDecodeError, TypeError):
            row["thread"] = []
    return rows


@app.get("/api/activity")
async def get_activity():
    rows = await query_db("SELECT * FROM actions ORDER BY created_at DESC LIMIT 100")
    for row in rows:
        try:
            row["details"] = json.loads(row.get("details_json", "{}"))
        except (json.JSONDecodeError, TypeError):
            row["details"] = {}
    return rows


# ── Dashboard UI ──


# ── Discovery: the provider menu, an estimate, and a run ──
#
# This is the only stage that spends money, so the UI never starts one without
# showing what it will cost first.

_discovery_task: asyncio.Task | None = None
_discovery_report: dict | None = None


def _discovery_queries(body: dict, config):
    from mercury.collectors.discover import build_queries

    cities = [c.strip() for c in (body.get("cities") or []) if c.strip()] or None
    return build_queries(
        config, cities=cities,
        depth=int(body.get("depth") or 30),
        limit=int(body.get("limit") or 100),
    )


@app.get("/api/discover/providers")
async def get_discovery_providers():
    """What each source does, what it costs, and whether it's ready to use."""
    try:
        from mercury.collectors.discover import DEFAULT_PROVIDER, provider_menu
        from mercury.config import load_env

        state = _state()
        await state.init_db()
        return {
            "providers": provider_menu(load_env().model_dump()),
            "default": DEFAULT_PROVIDER,
            "selected": await state.get_setting("discovery_provider") or DEFAULT_PROVIDER,
            "paused": await state.get_setting("discovery_paused"),
            "running": bool(_discovery_task and not _discovery_task.done()),
            "last_report": _discovery_report,
        }
    except Exception as e:
        logger.exception("discovery providers failed")
        return {"providers": [], "error": str(e)}


@app.post("/api/discover/estimate")
async def estimate_discovery(request: Request):
    """Projected spend and the exact query list, before anything is called."""
    try:
        from mercury.collectors.discover import PROVIDERS, estimate_cost
        from mercury.config import load_config

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"error": f"unknown provider {provider!r}"},
                                status_code=400)

        queries = _discovery_queries(body, load_config())
        return {
            "provider": provider,
            "queries": [q.keyword() for q in queries],
            "query_count": len(queries),
            "estimated_cost": round(estimate_cost(provider, queries), 4),
            "free": PROVIDERS[provider].estimate(queries) == 0,
        }
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/discover/run")
async def start_discovery(request: Request):
    """Kick off a run in the background and hand back immediately.

    Discovery takes minutes, not milliseconds — holding the request open
    would just time out. Progress shows up in the run log.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from mercury.collectors.discover import PROVIDERS
        from mercury.config import load_config
        from mercury.pipeline import run_prospecting

        body = await request.json()
        provider = body.get("provider") or ""
        if provider not in PROVIDERS:
            return JSONResponse({"success": False,
                                 "message": f"unknown provider {provider!r}"},
                                status_code=400)

        config = load_config()
        queries = _discovery_queries(body, config)
        max_spend = float(body.get("max_spend") or 1.0)

        state = _state()
        await state.init_db()
        await state.set_setting("discovery_provider", provider)
        await state.set_setting("discovery_paused", "")

        async def _go():
            global _discovery_report
            try:
                # Discovery chains straight into profiling: reading the sites
                # is free, and it is what makes the results worth anything.
                result = await run_prospecting(state, config, provider, queries,
                                               max_spend=max_spend)
                _discovery_report = {
                    **(result.discover or {}),
                    "profiled_companies": result.profiled_companies,
                    "profile_observations": result.profile_observations,
                    "errors": result.errors,
                }
            except Exception as exc:
                logger.exception("discovery run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "queries": len(queries)}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/profile/run")
async def start_profile():
    """Read the websites of everything discovered but not yet looked at.

    Free and model-free, so there is nothing to estimate and no cap to set.
    """
    global _discovery_task, _discovery_report

    if _discovery_task and not _discovery_task.done():
        return JSONResponse({"success": False, "message": "a run is already going"},
                            status_code=409)
    try:
        from mercury.pipeline import run_profile_stage

        state = _state()
        await state.init_db()
        pending = await state.count_companies_needing_profile()
        if not pending:
            return {"success": True, "pending": 0}

        async def _go():
            global _discovery_report
            try:
                companies, observations, _ = await run_profile_stage(state, limit=200)
                _discovery_report = {
                    "profiled_companies": companies,
                    "profile_observations": observations,
                }
            except Exception as exc:
                logger.exception("profile run failed")
                _discovery_report = {"errors": [str(exc)], "stopped": "failed"}

        _discovery_report = None
        _discovery_task = asyncio.create_task(_go())
        return {"success": True, "pending": pending}
    except Exception as e:
        return JSONResponse({"success": False, "message": str(e)}, status_code=500)


@app.post("/api/discover/stop")
async def stop_discovery():
    """Kill switch. Read between batches, so an in-flight run stops cleanly."""
    try:
        state = _state()
        await state.init_db()
        await state.set_setting("discovery_paused", "stopped from dashboard")
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
        paused = await state.get_setting("sending_paused")
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
        if paused:
            items.append({
                "key": "paused", "tone": "bad",
                "title": "Sending is paused",
                "detail": str(paused) + ". Nothing will go out until you resume it.",
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

        if pending:
            items.append({
                "key": "outbox", "tone": "warn",
                "title": (f"{len(pending)} email waiting for approval" if len(pending) == 1
                          else f"{len(pending)} emails waiting for approval"),
                "detail": "Nothing sends until you approve it. Read them one at a time.",
                "action": "Open the decisions desk", "tab": "outbox",
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

CATEGORY_META = {
    "discovery": {
        "label": "Discovery — who exists",
        "blurb": "How Mercury finds businesses at all, and how visible they are. "
                 "This is the only stage that costs money.",
    },
    "profile": {
        "label": "Profile — what they are",
        "blurb": "Read from the pages a business already publishes. Free, no AI "
                 "tokens, three HTTP requests per company. These are the signals "
                 "that make an email specific.",
    },
    "people": {
        "label": "People — who decides",
        "blurb": "Named humans and whether they're the one who can say yes.",
    },
    "verification": {
        "label": "Contactability — can you reach them",
        "blurb": "Whether the address will actually deliver, and what to do when "
                 "it won't.",
    },
}
CATEGORY_ORDER = ["discovery", "profile", "people", "verification"]


@app.get("/api/signals")
async def get_signals():
    """The signal vocabulary, grouped for review, with live cohort sizes."""
    try:
        from mercury.signals import seed_signal_catalog

        state = _state()
        await state.init_db()
        # Seeding is idempotent and never overrides a decision the user made,
        # so it is safe to run on every load — new signals shipped in an
        # upgrade show up as `proposed` without any migration step.
        await seed_signal_catalog(state)

        codes = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}

        groups, summary = [], {"proposed": 0, "confirmed": 0, "rejected": 0}
        for cat in CATEGORY_ORDER:
            rows = []
            for sig in codes:
                if sig.get("category") != cat:
                    continue
                seen = counts.get(sig["code"], {})
                rows.append({
                    **sig,
                    "companies": seen.get("companies", 0),
                    "observations": seen.get("observations", 0),
                })
            if rows:
                meta = CATEGORY_META.get(cat, {})
                groups.append({
                    "key": cat,
                    "label": meta.get("label", cat.title()),
                    "blurb": meta.get("blurb", ""),
                    "signals": rows,
                })
        for sig in codes:
            summary[sig.get("status", "proposed")] = (
                summary.get(sig.get("status", "proposed"), 0) + 1
            )

        return {"summary": summary, "groups": groups, "total": len(codes)}
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

    The payoff for confirming signals: a cohort is a query, not a list. Set
    intersection happens in SQL — intersecting in JS over a capped fetch
    silently returns the wrong answer.
    """
    try:
        body = await request.json()
        require = [c for c in (body.get("require") or []) if c]
        exclude = [c for c in (body.get("exclude") or []) if c]
        if not require:
            return {"size": 0, "companies": []}

        state = _state()
        await state.init_db()
        ids = await state.cohort(require, exclude, limit=1000)
        if not ids:
            return {"size": 0, "companies": []}

        placeholders = ",".join("?" for _ in ids[:200])
        rows = await query_db(
            f"SELECT id, name, domain, industry, location FROM companies "
            f"WHERE id IN ({placeholders})",
            tuple(ids[:200]),
        )
        return {"size": len(ids), "companies": rows}
    except Exception as e:
        return JSONResponse({"size": 0, "companies": [], "error": str(e)}, status_code=500)


@app.get("/api/runs")
async def get_runs_api():
    """The collector run log — what ran, when, what it produced and cost."""
    try:
        state = _state()
        await state.init_db()
        await state.sweep_stale_runs()
        return await state.get_runs(limit=25)
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
