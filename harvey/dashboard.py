"""Harvey Dashboard — local web UI to set up, control, and monitor Harvey."""

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

import aiosqlite
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger("harvey.dashboard")

from harvey.paths import PROJECT_ROOT  # noqa: E402
DB_PATH = PROJECT_ROOT / "data" / "harvey.db"
ENV_FILE = PROJECT_ROOT / ".env"
CONFIG_FILE = PROJECT_ROOT / "harvey.yaml"
PID_FILE = PROJECT_ROOT / "data" / "harvey.pid"
LOG_FILE = PROJECT_ROOT / "data" / "harvey.log"

app = FastAPI(title="Harvey Dashboard")

# Harvey process tracking
_harvey_process: subprocess.Popen | None = None
_harvey_started_at: datetime | None = None
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
    env_vars = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                env_vars[key.strip()] = value.strip()
    return env_vars


def _write_env_file(updates: dict[str, str]):
    """Update .env file with new values, preserving existing entries."""
    existing = _read_env_file()
    existing.update(updates)
    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    load_dotenv(str(ENV_FILE), override=True)


def _check_harvey_pid() -> int | None:
    """Check if there's a running Harvey process from a PID file."""
    global _harvey_process, _harvey_started_at
    if _harvey_process and _harvey_process.poll() is None:
        return _harvey_process.pid
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

    # 3. Instantly API key
    instantly_key = env_vars.get("INSTANTLY_API_KEY", "") or os.getenv("INSTANTLY_API_KEY", "")
    instantly_set = bool(instantly_key) and instantly_key != "your_instantly_api_key_here"
    checks.append({
        "id": "instantly_key", "label": "Instantly API key",
        "done": instantly_set,
        "required": True,
        "help": "Get your API key from Instantly Settings > Integrations. Enter it in the Settings tab.",
    })

    # 4. Instantly API working
    instantly_works = False
    if instantly_set:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    "https://api.instantly.ai/api/v2/accounts",
                    headers={"Authorization": f"Bearer {instantly_key}"},
                )
                instantly_works = resp.status_code == 200
        except Exception:
            pass
    checks.append({
        "id": "instantly_works", "label": "Instantly API connected",
        "done": instantly_works,
        "required": True,
        "help": "Your Instantly API key isn't working. Check that it's correct and you have the Growth plan.",
    })

    # 5. Config valid
    config_valid = False
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = yaml.safe_load(f)
            company = cfg.get("persona", {}).get("company", "")
            product = cfg.get("product", {}).get("name", "")
            config_valid = company not in ("Your Company", "") and product not in ("Your Product", "")
        except Exception:
            pass
    checks.append({
        "id": "config", "label": "Harvey configured (harvey.yaml)",
        "done": config_valid,
        "required": True,
        "help": "Train Harvey on your product. Use the trainer or set up manually through Claude.",
    })

    # 6. Product trained
    product_trained = (PROJECT_ROOT / "skills" / "product_knowledge.md").exists()
    checks.append({
        "id": "product_trained", "label": "Product knowledge trained",
        "done": product_trained,
        "required": True,
        "help": "Run: harvey train https://yourwebsite.com (or set up through Claude).",
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


@app.get("/api/settings")
async def get_settings():
    """Get current settings (API keys masked)."""
    env_vars = _read_env_file()
    # Also check os.environ as fallback
    for key in ["INSTANTLY_API_KEY", "LINKEDIN_EMAIL", "LINKEDIN_PASSWORD",
                "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]:
        if key not in env_vars:
            env_vars[key] = os.getenv(key, "")

    return {
        "instantly_api_key": env_vars.get("INSTANTLY_API_KEY", ""),
        "instantly_api_key_masked": _mask_key(env_vars.get("INSTANTLY_API_KEY", "")),
        "linkedin_email": env_vars.get("LINKEDIN_EMAIL", ""),
        "linkedin_password_set": bool(env_vars.get("LINKEDIN_PASSWORD", "")),
        "cloudflare_account_id": env_vars.get("CLOUDFLARE_ACCOUNT_ID", ""),
        "cloudflare_api_token_masked": _mask_key(env_vars.get("CLOUDFLARE_API_TOKEN", "")),
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
                     "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_API_TOKEN"]:
            if key in data and data[key] is not None:
                # Strip newlines so a crafted value can't inject extra .env entries
                updates[key] = str(data[key]).replace("\n", " ").replace("\r", " ").strip()
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


# ── Harvey Controls ──


@app.get("/api/harvey/status")
async def get_harvey_status():
    """Check if Harvey is currently running."""
    pid = _check_harvey_pid()
    started = _harvey_started_at.isoformat() if _harvey_started_at else None
    return {"running": pid is not None, "pid": pid, "started_at": started}


@app.post("/api/harvey/start")
async def start_harvey():
    """Start Harvey's heartbeat loop as a subprocess."""
    global _harvey_process, _harvey_started_at

    if _check_harvey_pid():
        return {"success": False, "message": "Harvey is already running."}

    # Ensure data dir exists
    (PROJECT_ROOT / "data").mkdir(parents=True, exist_ok=True)

    try:
        log_handle = open(LOG_FILE, "a")
        try:
            _harvey_process = subprocess.Popen(
                [sys.executable, "-m", "harvey"],
                cwd=str(PROJECT_ROOT),
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
            )
        finally:
            # Child holds its own copies of the fds; don't leak ours.
            log_handle.close()
    except Exception as e:
        logger.warning("Failed to start Harvey: %s", e)
        return {"success": False, "message": f"Failed to start Harvey: {e}"}
    _harvey_started_at = datetime.now()

    # Write PID file
    try:
        PID_FILE.write_text(str(_harvey_process.pid))
    except OSError as e:
        logger.warning("Could not write PID file: %s", e)

    return {"success": True, "pid": _harvey_process.pid}


@app.post("/api/harvey/stop")
async def stop_harvey():
    """Stop the Harvey subprocess."""
    global _harvey_process, _harvey_started_at

    pid = _check_harvey_pid()
    if not pid:
        return {"success": False, "message": "Harvey is not running."}

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

    _harvey_process = None
    _harvey_started_at = None
    PID_FILE.unlink(missing_ok=True)

    return {"success": True}


@app.get("/api/harvey/logs")
async def get_harvey_logs():
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
            f"GROUP BY {alias} ORDER BY cost_usd DESC LIMIT 25"
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
        from harvey.integrations.quota import QuotaClient
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


@app.get("/api/export/prospects.csv")
async def export_prospects(all: bool = False, min_score: int = 0, email_status: str = ""):
    """Sequencer-ready CSV download of the prospect list."""
    from fastapi.responses import PlainTextResponse
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

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


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Harvey — Command Deck</title>
<style>
  :root {
    --bg: #08090c;
    --panel: #0f1216;
    --panel-raised: #141922;
    --border: #1d2430;
    --border-strong: #2b3444;
    --text: #e9ecf2;
    --text-2: #9aa4b4;
    --text-3: #5c6774;
    --accent: #3ecf8e;
    --accent-deep: #22996a;
    --accent-soft: rgba(62, 207, 142, 0.12);
    --blue: #74a8ff;
    --amber: #e5b567;
    --red: #e06c75;
    --purple: #b48ce8;
    --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Inter, sans-serif;
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  ::selection { background: rgba(62,207,142,0.25); }

  html { color-scheme: dark; }

  body {
    font-family: var(--sans);
    background: var(--bg);
    background-image:
      radial-gradient(1100px 480px at 75% -12%, rgba(62,207,142,0.06), transparent 60%),
      radial-gradient(900px 420px at 8% -10%, rgba(116,168,255,0.05), transparent 55%);
    background-repeat: no-repeat;
    color: var(--text);
    min-height: 100vh;
    font-size: 14px;
    -webkit-font-smoothing: antialiased;
  }

  a { color: var(--blue); text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* ── Header ── */
  header {
    position: sticky; top: 0; z-index: 100;
    background: rgba(8,9,12,0.82);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid var(--border);
    padding: 14px 32px;
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
  }

  .brand { display: flex; align-items: center; gap: 12px; }
  .brand .mark {
    width: 34px; height: 34px; border-radius: 9px;
    background: linear-gradient(145deg, #2fbf82, #17795a);
    box-shadow: 0 0 0 1px rgba(62,207,142,0.35), 0 4px 14px rgba(62,207,142,0.18);
    display: flex; align-items: center; justify-content: center;
    font-weight: 800; font-size: 17px; color: #04140d; letter-spacing: -0.5px;
  }
  .brand h1 { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; line-height: 1.1; }
  .brand .tagline {
    font-size: 10px; text-transform: uppercase; letter-spacing: 1.4px;
    color: var(--text-3); margin-top: 2px; font-weight: 600;
  }

  .header-controls { display: flex; align-items: center; gap: 10px; }

  .harvey-status {
    display: flex; align-items: center; gap: 8px;
    font-size: 12px; font-weight: 600; color: var(--text-2);
    padding: 7px 14px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 99px;
  }
  .status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .status-dot.running {
    background: var(--accent);
    box-shadow: 0 0 0 0 rgba(62,207,142,0.5);
    animation: pulse 2s infinite;
  }
  .status-dot.stopped { background: #4a5361; }
  .status-dot.offline { background: var(--red); }
  @keyframes pulse {
    0% { box-shadow: 0 0 0 0 rgba(62,207,142,0.45); }
    70% { box-shadow: 0 0 0 7px rgba(62,207,142,0); }
    100% { box-shadow: 0 0 0 0 rgba(62,207,142,0); }
  }

  .refresh-btn {
    background: var(--panel); border: 1px solid var(--border); color: var(--text-2);
    padding: 7px 14px; border-radius: 99px; cursor: pointer;
    font-size: 12px; font-weight: 600; font-family: inherit;
    transition: border-color .15s, color .15s;
  }
  .refresh-btn:hover { border-color: var(--border-strong); color: var(--text); }

  /* ── Nav ── */
  nav {
    position: sticky; top: 63px; z-index: 99;
    background: rgba(8,9,12,0.82);
    backdrop-filter: blur(14px);
    -webkit-backdrop-filter: blur(14px);
    border-bottom: 1px solid var(--border);
    padding: 0 24px; display: flex; overflow-x: auto;
    scrollbar-width: none;
  }
  nav::-webkit-scrollbar { display: none; }

  nav button {
    position: relative;
    background: none; border: none;
    color: var(--text-3); padding: 13px 14px; cursor: pointer;
    font-size: 13px; font-weight: 500; font-family: inherit;
    transition: color .15s; white-space: nowrap;
  }
  nav button:hover { color: var(--text-2); }
  nav button.active { color: var(--text); font-weight: 600; }
  nav button.active::after {
    content: ""; position: absolute; left: 14px; right: 14px; bottom: -1px;
    height: 2px; border-radius: 2px 2px 0 0; background: var(--accent);
  }

  main { padding: 28px 32px 64px; max-width: 1400px; margin: 0 auto; }

  .section { display: none; }
  .section.active { display: block; animation: rise .25s ease; }
  @keyframes rise { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: none; } }

  .section-head { margin-bottom: 20px; }
  .section-head h2 { font-size: 20px; font-weight: 700; letter-spacing: -0.4px; }
  .section-head p { font-size: 13px; color: var(--text-3); margin-top: 4px; }

  /* ── Cards ── */
  .card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; margin-bottom: 16px;
  }
  .card h2 { font-size: 15px; font-weight: 650; letter-spacing: -0.2px; margin-bottom: 16px; }

  /* ── Stat cards ── */
  .stats-grid {
    display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
    gap: 14px; margin-bottom: 28px;
  }
  .stat-card {
    position: relative; overflow: hidden;
    background: linear-gradient(180deg, var(--panel-raised), var(--panel));
    border: 1px solid var(--border); border-radius: 12px; padding: 20px;
    transition: border-color .2s;
  }
  .stat-card:hover { border-color: var(--border-strong); }
  .stat-card::before {
    content: ""; position: absolute; top: 0; left: 20px; right: 20px; height: 1px;
    background: linear-gradient(90deg, transparent, rgba(62,207,142,0.35), transparent);
  }
  .stat-card .label {
    font-size: 10.5px; text-transform: uppercase; letter-spacing: 1.2px;
    color: var(--text-3); font-weight: 700; margin-bottom: 10px;
  }
  .stat-card .value {
    font-size: 34px; font-weight: 750; letter-spacing: -1px; line-height: 1;
    font-variant-numeric: tabular-nums;
  }
  .stat-card .breakdown { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 5px; }
  .chip {
    font-size: 11px; font-weight: 550; color: var(--text-2);
    background: rgba(255,255,255,0.04); border: 1px solid var(--border);
    padding: 2px 9px; border-radius: 99px; font-variant-numeric: tabular-nums;
  }
  .chip b { color: var(--text); font-weight: 650; }

  /* ── Progress ── */
  .progress-wrap { margin-bottom: 24px; }
  .progress-label { display: flex; justify-content: space-between; margin-bottom: 8px; font-size: 13px; }
  .progress-label .pct { color: var(--text); font-weight: 700; font-variant-numeric: tabular-nums; }
  .progress-label .text { color: var(--text-3); }
  .progress-bar { background: rgba(255,255,255,0.05); border-radius: 99px; height: 8px; overflow: hidden; }
  .progress-fill { height: 100%; border-radius: 99px; transition: width .5s ease; }
  .progress-fill.green { background: linear-gradient(90deg, var(--accent-deep), var(--accent)); }
  .progress-fill.yellow { background: linear-gradient(90deg, #a3801f, var(--amber)); }

  /* ── Setup checklist ── */
  .check-item {
    display: flex; align-items: flex-start; gap: 14px;
    padding: 14px 0; border-bottom: 1px solid var(--border);
  }
  .check-item:last-child { border-bottom: none; }
  .check-icon {
    width: 22px; height: 22px; border-radius: 50%; flex-shrink: 0;
    display: flex; align-items: center; justify-content: center;
    font-size: 12px; margin-top: 1px;
  }
  .check-icon.done { background: var(--accent-soft); color: var(--accent); border: 1px solid rgba(62,207,142,0.3); }
  .check-icon.pending { background: transparent; color: var(--text-3); border: 1px dashed var(--border-strong); }
  .check-info { flex: 1; }
  .check-label { font-size: 14px; font-weight: 550; color: var(--text); }
  .check-label.done { color: var(--text-3); text-decoration: line-through; text-decoration-color: rgba(255,255,255,0.15); }
  .check-help { font-size: 12.5px; color: var(--text-3); margin-top: 4px; line-height: 1.5; }
  .optional-tag {
    font-size: 9.5px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.8px;
    background: rgba(116,168,255,0.1); color: var(--blue);
    padding: 2px 8px; border-radius: 99px; margin-left: 8px; vertical-align: 1px;
  }
  .subhead {
    margin: 20px 0 4px; font-size: 10.5px; color: var(--text-3);
    text-transform: uppercase; letter-spacing: 1.2px; font-weight: 700;
  }

  /* ── Forms ── */
  .form-group { margin-bottom: 16px; }
  .form-label {
    display: block; font-size: 11px; color: var(--text-2); margin-bottom: 7px;
    text-transform: uppercase; letter-spacing: 0.8px; font-weight: 650;
  }
  .form-input {
    width: 100%; padding: 10px 14px;
    background: rgba(255,255,255,0.03); border: 1px solid var(--border-strong);
    border-radius: 8px; color: var(--text); font-size: 14px; font-family: inherit;
    transition: border-color .15s, box-shadow .15s;
  }
  .form-input:focus { outline: none; border-color: var(--accent-deep); box-shadow: 0 0 0 3px rgba(62,207,142,0.12); }
  .form-input::placeholder { color: var(--text-3); }
  .form-row { display: flex; gap: 10px; align-items: flex-end; }
  .form-row .form-group { flex: 1; }
  .card .lede { font-size: 13px; color: var(--text-2); margin: -6px 0 18px; line-height: 1.6; }

  .btn {
    padding: 9px 20px; border: 1px solid transparent; border-radius: 8px;
    font-size: 13px; font-weight: 600; cursor: pointer; font-family: inherit;
    transition: background .15s, border-color .15s, transform .05s;
  }
  .btn:active { transform: translateY(1px); }
  .btn:disabled { opacity: 0.5; cursor: not-allowed; }
  .btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
  .btn-primary { background: var(--accent-deep); color: #eafff5; box-shadow: inset 0 1px 0 rgba(255,255,255,0.12); }
  .btn-primary:hover:not(:disabled) { background: #2ab27c; }
  .btn-danger { background: #8a2f36; color: #ffe9ea; }
  .btn-danger:hover:not(:disabled) { background: #a13940; }
  .btn-secondary { background: rgba(255,255,255,0.03); border-color: var(--border-strong); color: var(--text-2); }
  .btn-secondary:hover:not(:disabled) { color: var(--text); border-color: #3a4557; }
  .btn-sm { padding: 6px 13px; font-size: 12px; }
  .btn-group { display: flex; gap: 10px; margin-top: 16px; }

  .test-result { font-size: 13px; margin-top: 10px; padding: 9px 13px; border-radius: 8px; }
  .test-result.success { background: var(--accent-soft); color: var(--accent); border: 1px solid rgba(62,207,142,0.25); }
  .test-result.error { background: rgba(224,108,117,0.1); color: var(--red); border: 1px solid rgba(224,108,117,0.25); }
  .test-result.pending { background: rgba(255,255,255,0.04); color: var(--text-2); border: 1px solid var(--border); }

  /* ── Controls ── */
  .control-panel { display: grid; grid-template-columns: 1fr 1.4fr; gap: 16px; }
  @media (max-width: 900px) { .control-panel { grid-template-columns: 1fr; } }

  .status-big { display: flex; align-items: center; gap: 12px; margin-bottom: 12px; }
  .status-big .dot { width: 13px; height: 13px; border-radius: 50%; }
  .status-big .dot.running { background: var(--accent); animation: pulse 2s infinite; }
  .status-big .dot.stopped { background: #4a5361; }
  .status-big .label { font-size: 19px; font-weight: 700; letter-spacing: -0.3px; }
  .status-big .label.running { color: var(--accent); }
  .status-big .label.stopped { color: var(--text-2); }
  .status-meta { font-size: 12px; color: var(--text-3); margin-bottom: 18px; font-variant-numeric: tabular-nums; min-height: 15px; }

  .log-viewer {
    background: #07080a; border: 1px solid var(--border); border-radius: 10px;
    padding: 14px 16px; font-family: var(--mono);
    font-size: 11.5px; color: #8fa39a; line-height: 1.65;
    max-height: 420px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
  }

  /* ── Help ── */
  .help-section { margin-bottom: 28px; }
  .help-section h2 { font-size: 17px; font-weight: 700; letter-spacing: -0.3px; margin-bottom: 10px; }
  .help-section p { font-size: 14px; color: var(--text-2); line-height: 1.7; margin-bottom: 10px; }
  .help-section code { background: rgba(255,255,255,0.05); padding: 2px 7px; border-radius: 5px; font-size: 12.5px; font-family: var(--mono); color: var(--text); }
  .help-section pre {
    background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; font-size: 12.5px; font-family: var(--mono); color: var(--text-2); line-height: 1.7;
    overflow-x: auto; margin: 12px 0;
  }

  .file-table { width: 100%; font-size: 13px; border-collapse: collapse; }
  .file-table td { padding: 9px 12px; border-bottom: 1px solid var(--border); }
  .file-table tr:last-child td { border-bottom: none; }
  .file-table td:first-child { color: var(--text); font-family: var(--mono); font-size: 12px; white-space: nowrap; width: 220px; }
  .file-table td:last-child { color: var(--text-2); }

  details { margin-bottom: 8px; }
  details summary {
    cursor: pointer; padding: 12px 16px; background: var(--panel); border: 1px solid var(--border);
    border-radius: 10px; font-size: 13.5px; font-weight: 550; color: var(--text-2); list-style: none;
    transition: color .15s;
  }
  details summary:hover { color: var(--text); }
  details summary::-webkit-details-marker { display: none; }
  details summary::before { content: "+"; display: inline-block; width: 18px; color: var(--accent); font-weight: 700; }
  details[open] summary::before { content: "–"; }
  details[open] summary { border-radius: 10px 10px 0 0; border-bottom: none; color: var(--text); }
  details .faq-body {
    padding: 4px 16px 16px 34px; background: var(--panel); border: 1px solid var(--border); border-top: none;
    border-radius: 0 0 10px 10px; font-size: 13px; color: var(--text-2); line-height: 1.7;
  }

  /* ── Toast ── */
  .toast {
    position: fixed; bottom: 24px; right: 24px; padding: 12px 20px;
    border-radius: 10px; font-size: 13px; font-weight: 550; z-index: 1000;
    box-shadow: 0 12px 32px rgba(0,0,0,0.5);
    animation: toastIn .2s ease, toastOut .3s 2.2s forwards;
  }
  .toast.success { background: #0d2a1d; color: var(--accent); border: 1px solid rgba(62,207,142,0.4); }
  .toast.error { background: #2c1416; color: var(--red); border: 1px solid rgba(224,108,117,0.4); }
  @keyframes toastIn { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; } }
  @keyframes toastOut { from { opacity: 1; } to { opacity: 0; transform: translateY(6px); } }

  /* ── Tables ── */
  .table-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    overflow-x: auto;
  }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th {
    text-align: left; padding: 11px 16px; border-bottom: 1px solid var(--border);
    color: var(--text-3); font-weight: 700; font-size: 10.5px;
    text-transform: uppercase; letter-spacing: 1px; white-space: nowrap;
    background: rgba(255,255,255,0.015);
  }
  td {
    padding: 12px 16px; border-bottom: 1px solid var(--border); vertical-align: top;
    max-width: 300px; overflow: hidden; text-overflow: ellipsis; color: var(--text-2);
  }
  td:first-child { color: var(--text); font-weight: 550; }
  tr:last-child td { border-bottom: none; }
  tbody tr { transition: background .1s; }
  tbody tr:hover td { background: rgba(255,255,255,0.02); }
  .verified { color: var(--accent); }
  .email-tag { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; padding: 1px 6px; border-radius: 4px; margin-left: 6px; }
  .email-tag.verified { color: var(--accent); background: var(--accent-soft); }
  .email-tag.risky { color: var(--amber); background: rgba(229,181,103,0.12); }
  .email-tag.guess { color: var(--text-3); background: rgba(255,255,255,0.05); }
  .email-tag.invalid { color: var(--red); background: rgba(224,108,117,0.12); }
  .muted { color: var(--text-3); }

  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 10px; border-radius: 99px; font-size: 11px; font-weight: 650;
    border: 1px solid transparent; white-space: nowrap;
  }
  .badge::before { content: ""; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
  .badge-new { background: rgba(116,168,255,0.1); color: var(--blue); border-color: rgba(116,168,255,0.22); }
  .badge-contacted, .badge-open, .badge-objection { background: rgba(229,181,103,0.1); color: var(--amber); border-color: rgba(229,181,103,0.22); }
  .badge-replied, .badge-active, .badge-interested { background: var(--accent-soft); color: var(--accent); border-color: rgba(62,207,142,0.25); }
  .badge-meeting { background: rgba(180,140,232,0.12); color: var(--purple); border-color: rgba(180,140,232,0.25); }
  .badge-draft { background: rgba(255,255,255,0.05); color: var(--text-2); border-color: var(--border-strong); }
  .badge-closed { background: rgba(255,255,255,0.04); color: var(--text-3); border-color: var(--border); }
  .badge-lost, .badge-not_interested { background: rgba(224,108,117,0.1); color: var(--red); border-color: rgba(224,108,117,0.22); }
  .badge-unknown { background: rgba(255,255,255,0.04); color: var(--text-3); border-color: var(--border); }

  .campaign-card, .convo-card {
    background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
    padding: 24px; margin-bottom: 16px;
  }
  .campaign-card h3, .convo-card h3 { font-size: 16px; font-weight: 700; letter-spacing: -0.2px; margin-bottom: 6px; }
  .campaign-card .meta, .convo-card .meta {
    font-size: 12px; color: var(--text-3); margin-bottom: 18px;
    display: flex; gap: 14px; align-items: center; flex-wrap: wrap;
  }

  .email-step {
    border-left: 2px solid var(--border-strong); padding: 14px 20px; margin: 0 0 12px 6px;
    border-radius: 0 10px 10px 0; background: rgba(255,255,255,0.015);
  }
  .email-step .step-num {
    font-size: 10px; color: var(--accent); text-transform: uppercase;
    letter-spacing: 1px; font-weight: 700; margin-bottom: 7px;
  }
  .email-step .subject { font-size: 14px; font-weight: 650; margin-bottom: 8px; }
  .email-step .body { font-size: 13px; color: var(--text-2); line-height: 1.7; white-space: pre-wrap; }

  .thread-msg {
    padding: 12px 16px; margin-bottom: 8px; border-radius: 12px; max-width: 78%;
    font-size: 13px; line-height: 1.6; white-space: pre-wrap;
  }
  .thread-msg.sent { background: rgba(62,207,142,0.08); border: 1px solid rgba(62,207,142,0.16); color: #cfeee0; margin-left: auto; border-bottom-right-radius: 4px; }
  .thread-msg.received { background: rgba(255,255,255,0.04); border: 1px solid var(--border); color: var(--text-2); border-bottom-left-radius: 4px; }
  .thread-msg .sender { font-size: 10.5px; color: var(--text-3); margin-bottom: 5px; text-transform: uppercase; letter-spacing: 0.6px; font-weight: 700; }

  .activity-feed { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 8px 20px; }
  .activity-item { display: flex; gap: 18px; padding: 12px 0; border-bottom: 1px solid var(--border); font-size: 13px; align-items: baseline; }
  .activity-item:last-child { border-bottom: none; }
  .activity-item .time { color: var(--text-3); font-size: 12px; min-width: 130px; font-variant-numeric: tabular-nums; }
  .activity-item .agent {
    color: var(--accent); min-width: 84px; font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 0.8px;
  }
  .activity-item .action { color: var(--text-2); }

  /* ── Empty states ── */
  .empty {
    text-align: center; padding: 72px 24px;
    background: var(--panel); border: 1px dashed var(--border-strong); border-radius: 12px;
  }
  .empty .glyph {
    width: 46px; height: 46px; margin: 0 auto 18px; border-radius: 12px;
    background: rgba(255,255,255,0.03); border: 1px solid var(--border);
    display: flex; align-items: center; justify-content: center;
    font-size: 20px; color: var(--text-3);
  }
  .empty .title { font-size: 15px; font-weight: 650; color: var(--text); margin-bottom: 6px; }
  .empty .copy { font-size: 13px; color: var(--text-3); line-height: 1.6; max-width: 420px; margin: 0 auto; }
  .empty .copy b { color: var(--text-2); font-weight: 600; }

  @media (max-width: 700px) {
    header, main { padding-left: 18px; padding-right: 18px; }
    nav { padding: 0 10px; }
    .brand .tagline { display: none; }
  }
</style>
</head>
<body>

<header>
  <div class="brand">
    <div class="mark">H</div>
    <div>
      <h1>Harvey</h1>
      <div class="tagline">Always Be Closing</div>
    </div>
  </div>
  <div class="header-controls">
    <div class="harvey-status" id="header-status">
      <span class="status-dot stopped" id="header-dot"></span>
      <span id="header-status-text">Checking…</span>
    </div>
    <button class="refresh-btn" onclick="loadCurrentTab()">Refresh</button>
  </div>
</header>

<nav>
  <button class="active" onclick="showTab('setup', this)">Setup</button>
  <button onclick="showTab('overview', this)">Overview</button>
  <button onclick="showTab('companies', this)">Companies</button>
  <button onclick="showTab('prospects', this)">Contacts</button>
  <button onclick="showTab('campaigns', this)">Campaigns</button>
  <button onclick="showTab('conversations', this)">Conversations</button>
  <button onclick="showTab('activity', this)">Activity</button>
  <button onclick="showTab('usage', this)">Usage</button>
  <button onclick="showTab('settings', this)">Settings</button>
  <button onclick="showTab('controls', this)">Controls</button>
  <button onclick="showTab('help', this)">Help</button>
</nav>

<main>

<!-- Setup -->
<div id="setup" class="section active">
  <div class="section-head"><h2>Setup</h2><p>Everything Harvey needs before it can start closing.</p></div>
  <div class="card">
    <div class="progress-wrap" id="setup-progress"></div>
    <div id="setup-checklist"></div>
  </div>
</div>

<!-- Overview -->
<div id="overview" class="section">
  <div class="section-head"><h2>Pipeline Overview</h2><p>Live counts from Harvey's local database. Refreshes automatically.</p></div>
  <div class="stats-grid" id="stats-grid"></div>
</div>

<!-- Companies -->
<div id="companies" class="section">
  <div class="section-head"><h2>Companies</h2><p>Organizations Harvey has researched. Click a row to see its contacts.</p></div>
  <div id="companies-list"></div>
</div>

<!-- Contacts -->
<div id="prospects" class="section">
  <div class="section-head" style="display:flex;align-items:flex-end;justify-content:space-between;gap:16px">
    <div><h2>Contacts</h2><p>People Harvey has found and verified.</p></div>
    <div style="display:flex;gap:8px;flex-shrink:0">
      <a class="btn btn-secondary btn-sm" href="/api/export/prospects.csv" download>Export deliverable CSV</a>
      <a class="btn btn-secondary btn-sm" href="/api/export/prospects.csv?all=true" download>Export all</a>
    </div>
  </div>
  <div id="prospects-table"></div>
</div>

<!-- Campaigns -->
<div id="campaigns" class="section">
  <div class="section-head"><h2>Campaigns</h2><p>Email sequences Harvey has written and deployed.</p></div>
  <div id="campaigns-list"></div>
</div>

<!-- Conversations -->
<div id="conversations" class="section">
  <div class="section-head"><h2>Conversations</h2><p>Every reply, and how Harvey handled it.</p></div>
  <div id="conversations-list"></div>
</div>

<!-- Activity -->
<div id="activity" class="section">
  <div class="section-head"><h2>Activity</h2><p>A running log of every action Harvey's agents have taken.</p></div>
  <div id="activity-list"></div>
</div>

<!-- Usage -->
<div id="usage" class="section">
  <div class="section-head"><h2>Usage</h2><p>What Harvey is spending — real subscription quota, tokens, and equivalent API cost per agent and task.</p></div>
  <div id="usage-quota" class="card" style="display:none"></div>
  <div class="stats-grid" id="usage-stats"></div>
  <div class="card" id="usage-daily" style="display:none"></div>
  <div id="usage-tables"></div>
</div>

<!-- Settings -->
<div id="settings" class="section">
  <div class="section-head"><h2>Settings</h2><p>Credentials are stored locally in <span style="font-family:var(--mono);font-size:12px">.env</span> — never sent anywhere except the services themselves.</p></div>
  <div class="card">
    <h2>Instantly (Email Platform)</h2>
    <p class="lede">Required. Get your API key from <a href="https://app.instantly.ai/app/settings/integrations" target="_blank" rel="noopener">Instantly Settings &gt; Integrations</a>.</p>
    <div class="form-group">
      <label class="form-label" for="instantly-key">API Key</label>
      <div class="form-row">
        <div class="form-group" style="margin-bottom:0">
          <input type="password" class="form-input" id="instantly-key" placeholder="Enter your Instantly API key" autocomplete="off">
        </div>
        <button class="btn btn-secondary btn-sm" onclick="toggleVisibility('instantly-key')">Show</button>
        <button class="btn btn-secondary btn-sm" onclick="testInstantly()">Test</button>
      </div>
      <div id="instantly-test-result"></div>
    </div>
    <button class="btn btn-primary" onclick="saveInstantly()">Save</button>
  </div>

  <div class="card">
    <h2>LinkedIn <span class="optional-tag">optional</span></h2>
    <p class="lede">For automated LinkedIn prospecting. Harvey logs in and searches like a human.</p>
    <div class="form-group">
      <label class="form-label" for="linkedin-email">Email / Username</label>
      <input type="text" class="form-input" id="linkedin-email" placeholder="your@email.com" autocomplete="off">
    </div>
    <div class="form-group">
      <label class="form-label" for="linkedin-password">Password</label>
      <input type="password" class="form-input" id="linkedin-password" placeholder="Enter password" autocomplete="new-password">
    </div>
    <button class="btn btn-primary" onclick="saveLinkedIn()">Save</button>
  </div>

  <div class="card">
    <h2>Cloudflare <span class="optional-tag">optional</span></h2>
    <p class="lede">For deep website crawling with JavaScript rendering during product training. ~$5/month.</p>
    <div class="form-group">
      <label class="form-label" for="cf-account-id">Account ID</label>
      <input type="text" class="form-input" id="cf-account-id" placeholder="Your Cloudflare Account ID" autocomplete="off">
    </div>
    <div class="form-group">
      <label class="form-label" for="cf-api-token">API Token</label>
      <input type="password" class="form-input" id="cf-api-token" placeholder="Your Cloudflare API Token" autocomplete="off">
    </div>
    <button class="btn btn-primary" onclick="saveCloudflare()">Save</button>
  </div>
</div>

<!-- Controls -->
<div id="controls" class="section">
  <div class="section-head"><h2>Controls</h2><p>Start and stop Harvey's heartbeat loop, and watch what it's doing.</p></div>
  <div class="control-panel">
    <div class="card">
      <h2>Agent</h2>
      <div class="status-big" id="control-status">
        <div class="dot stopped" id="control-dot"></div>
        <span class="label stopped" id="control-label">Stopped</span>
      </div>
      <div class="status-meta" id="control-meta"></div>
      <div class="btn-group">
        <button class="btn btn-primary" id="btn-start" onclick="startHarvey()">Start Harvey</button>
        <button class="btn btn-danger" id="btn-stop" onclick="stopHarvey()" style="display:none">Stop Harvey</button>
      </div>
    </div>
    <div class="card">
      <h2>Recent Logs</h2>
      <div class="log-viewer" id="log-viewer">No logs yet. Start Harvey to see activity.</div>
      <div class="btn-group">
        <button class="btn btn-secondary btn-sm" onclick="loadLogs()">Refresh Logs</button>
      </div>
    </div>
  </div>
</div>

<!-- Help -->
<div id="help" class="section">
  <div class="section-head"><h2>Help</h2><p>What Harvey is, how it works, and how to fix the usual problems.</p></div>

  <div class="help-section">
    <h2>What is Harvey?</h2>
    <p>Harvey is an autonomous AI sales agent. Once set up, Harvey runs on its own: finds people who match your ideal customer, writes personalized cold emails, sends them through your email platform, reads every reply, handles objections, and works toward booking a meeting. You review everything through this dashboard.</p>
    <p>Harvey runs on your Claude Max subscription, so there are no extra AI costs. Everything stays on your machine in one folder.</p>
  </div>

  <div class="help-section">
    <h2>Getting Started</h2>
    <p>There are three things to do:</p>
    <pre>1. Go to the Settings tab and enter your Instantly API key
2. Train Harvey on your product (through Claude or the command line)
3. Go to the Controls tab and click Start</pre>
    <p>The Setup tab shows you exactly what's done and what still needs to happen.</p>
  </div>

  <div class="help-section">
    <h2>Where Everything Lives</h2>
    <p>Everything Harvey needs is inside this one project folder. Nothing is stored elsewhere.</p>
    <div class="table-card" style="padding:6px 4px">
    <table class="file-table">
      <tr><td>.env</td><td>Your API keys and credentials (never shared or committed)</td></tr>
      <tr><td>harvey.yaml</td><td>Your product info, target customers, and behavior settings</td></tr>
      <tr><td>skills/</td><td>Sales knowledge files. Edit these to change how Harvey writes and sells.</td></tr>
      <tr><td>skills/product_knowledge.md</td><td>Everything Harvey knows about your product (auto-generated from training)</td></tr>
      <tr><td>prompts/</td><td>Prompt templates for each agent. Advanced customization.</td></tr>
      <tr><td>data/harvey.db</td><td>Database with all prospects, campaigns, and conversations</td></tr>
      <tr><td>data/harvey.log</td><td>Log file showing what Harvey is doing</td></tr>
    </table>
    </div>
  </div>

  <div class="help-section">
    <h2>Getting Your API Keys</h2>

    <details>
      <summary>Instantly API Key (required)</summary>
      <div class="faq-body">
        <p>Instantly is the email platform Harvey uses to send campaigns.</p>
        <p>1. Sign up at <a href="https://instantly.ai" target="_blank" rel="noopener">instantly.ai</a> (you need the Growth plan for API access)</p>
        <p>2. Go to Settings &gt; Integrations</p>
        <p>3. Copy your API key</p>
        <p>4. Paste it in the Settings tab here</p>
      </div>
    </details>

    <details>
      <summary>LinkedIn Credentials (optional)</summary>
      <div class="faq-body">
        <p>If you want Harvey to find prospects on LinkedIn, enter your LinkedIn email and password. Harvey uses a real browser to search LinkedIn like a human would, with random delays and rate limits to avoid detection.</p>
        <p>If you skip this, Harvey will find prospects through Google searches and company website scraping instead.</p>
      </div>
    </details>

    <details>
      <summary>Cloudflare Browser Rendering (optional)</summary>
      <div class="faq-body">
        <p>This is only used during product training (when Harvey crawls your website to learn about your product). It handles JavaScript-heavy websites that a basic crawler can't read.</p>
        <p>1. Sign up at <a href="https://dash.cloudflare.com" target="_blank" rel="noopener">Cloudflare</a> (paid Workers plan, ~$5/month)</p>
        <p>2. Go to Workers &amp; Pages &gt; Browser Rendering</p>
        <p>3. Create an API token with Browser Rendering Edit permissions</p>
        <p>4. Enter your Account ID and API Token in the Settings tab</p>
        <p>Without this, Harvey uses a built-in crawler that works fine for most websites but can't render JavaScript.</p>
      </div>
    </details>
  </div>

  <div class="help-section">
    <h2>Common Issues</h2>

    <details>
      <summary>"command not found: harvey"</summary>
      <div class="faq-body">You need to activate the virtual environment first: <code>source .venv/bin/activate</code></div>
    </details>

    <details>
      <summary>Instantly API returns 401</summary>
      <div class="faq-body">Your API key is wrong, or you need the Growth plan (the free plan doesn't include API access). Double-check the key in Settings &gt; Integrations in your Instantly dashboard.</div>
    </details>

    <details>
      <summary>Claude headless mode fails</summary>
      <div class="faq-body">Make sure you've run <code>claude login</code> in your terminal and have an active Claude Max subscription. Harvey uses your existing subscription, not a separate API key.</div>
    </details>

    <details>
      <summary>Harvey isn't finding prospects</summary>
      <div class="faq-body">Check that your ICP (ideal customer profile) in harvey.yaml has realistic titles, industries, and geography. If LinkedIn is set up, check that the credentials are correct. Check the Activity tab to see what Harvey has been trying to do.</div>
    </details>
  </div>
</div>

</main>

<script>
let currentTab = 'setup';
let companyDrill = false;      // true while viewing a single company's contacts
let _companies = [], _prospects = [], _campaigns = [];

// ── Utilities ──

function escHtml(s) {
  if (s === null || s === undefined || s === '') return '';
  return String(s).replace(/[&<>"']/g, ch => (
    {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]
  ));
}

function badge(status) {
  const safe = String(status || 'unknown');
  const cls = 'badge-' + safe.toLowerCase().replace(/[^a-z0-9]+/g, '_');
  return '<span class="badge ' + escHtml(cls) + '">' + escHtml(safe) + '</span>';
}

function formatDate(d) {
  if (!d) return '';
  try {
    const dt = new Date(d);
    if (isNaN(dt)) return escHtml(d);
    return dt.toLocaleString('en-US', {month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
  } catch { return escHtml(d); }
}

function emptyState(glyph, title, copy) {
  return '<div class="empty"><div class="glyph">' + glyph + '</div>' +
    '<div class="title">' + title + '</div>' +
    '<div class="copy">' + copy + '</div></div>';
}

function offlineState() {
  return emptyState('&#9888;', 'Dashboard can\'t reach the server',
    'The dashboard process may have stopped. Restart it with <b>harvey dashboard</b> and refresh this page.');
}

async function api(path, opts) {
  try {
    const r = await fetch(path, opts);
    if (!r.ok) return null;
    return await r.json();
  } catch {
    return null;
  }
}

function showToast(msg, type) {
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 2600);
}

function toggleVisibility(inputId) {
  const el = document.getElementById(inputId);
  el.type = el.type === 'password' ? 'text' : 'password';
}

// ── Tabs ──

function showTab(id, btn) {
  currentTab = id;
  if (id === 'companies') companyDrill = false;
  document.querySelectorAll('.section').forEach(s => s.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b => b.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  if (btn) btn.classList.add('active');
  loadCurrentTab();
}

function loadCurrentTab() {
  switch (currentTab) {
    case 'setup': loadSetupStatus(); break;
    case 'overview': loadStats(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'settings': loadSettings(); break;
    case 'controls': loadHarveyStatus(); loadLogs(); break;
  }
}

// ── Setup ──

async function loadSetupStatus() {
  const data = await api('/api/setup-status');
  if (!data || !data.checks) {
    document.getElementById('setup-progress').innerHTML = '';
    document.getElementById('setup-checklist').innerHTML = offlineState();
    return;
  }
  const pct = data.percent || 0;
  const color = pct === 100 ? 'green' : 'yellow';

  document.getElementById('setup-progress').innerHTML =
    '<div class="progress-label">' +
      '<span class="pct">' + pct + '% complete</span>' +
      '<span class="text">' + data.completed + ' of ' + data.total_required + ' required steps done</span>' +
    '</div>' +
    '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>';

  const renderCheck = (c, optional) => {
    const icon = c.done
      ? '<span class="check-icon done">&#10003;</span>'
      : '<span class="check-icon pending">&#9679;</span>';
    return '<div class="check-item">' + icon +
      '<div class="check-info">' +
        '<div class="check-label ' + (c.done ? 'done' : '') + '">' + escHtml(c.label) +
          (optional ? ' <span class="optional-tag">optional</span>' : '') + '</div>' +
        (!c.done ? '<div class="check-help">' + escHtml(c.help) + '</div>' : '') +
      '</div></div>';
  };

  let html = data.checks.filter(c => c.required).map(c => renderCheck(c, false)).join('');
  const optional = data.checks.filter(c => !c.required);
  if (optional.length) {
    html += '<div class="subhead">Optional</div>';
    html += optional.map(c => renderCheck(c, true)).join('');
  }
  document.getElementById('setup-checklist').innerHTML = html;
}

// ── Settings ──

async function loadSettings() {
  const data = await api('/api/settings');
  if (!data) return;
  document.getElementById('instantly-key').value = data.instantly_api_key || '';
  document.getElementById('linkedin-email').value = data.linkedin_email || '';
  document.getElementById('linkedin-password').value = '';
  document.getElementById('cf-account-id').value = data.cloudflare_account_id || '';
  document.getElementById('cf-api-token').value = '';
  if (data.linkedin_password_set) {
    document.getElementById('linkedin-password').placeholder = 'Password saved (enter new to change)';
  }
}

async function saveEnv(payload, okMsg) {
  const data = await api('/api/settings/env', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload)
  });
  if (data && data.success) showToast(okMsg, 'success');
  else showToast((data && data.message) || 'Save failed — is the dashboard still running?', 'error');
}

function saveInstantly() {
  saveEnv({INSTANTLY_API_KEY: document.getElementById('instantly-key').value.trim()}, 'Instantly API key saved.');
}

function saveLinkedIn() {
  const payload = {LINKEDIN_EMAIL: document.getElementById('linkedin-email').value.trim()};
  const pass = document.getElementById('linkedin-password').value;
  if (pass) payload.LINKEDIN_PASSWORD = pass;
  saveEnv(payload, 'LinkedIn credentials saved.');
}

function saveCloudflare() {
  saveEnv({
    CLOUDFLARE_ACCOUNT_ID: document.getElementById('cf-account-id').value.trim(),
    CLOUDFLARE_API_TOKEN: document.getElementById('cf-api-token').value.trim()
  }, 'Cloudflare credentials saved.');
}

async function testInstantly() {
  const key = document.getElementById('instantly-key').value.trim();
  const el = document.getElementById('instantly-test-result');
  el.innerHTML = '<div class="test-result pending">Testing&hellip;</div>';
  const data = await api('/api/settings/test-instantly', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({api_key: key})
  });
  if (!data) {
    el.innerHTML = '<div class="test-result error">Could not reach the dashboard server.</div>';
    return;
  }
  el.innerHTML = '<div class="test-result ' + (data.success ? 'success' : 'error') + '">' + escHtml(data.message) + '</div>';
}

// ── Controls ──

async function loadHarveyStatus() {
  const data = await api('/api/harvey/status');
  const headerDot = document.getElementById('header-dot');
  const headerText = document.getElementById('header-status-text');

  if (!data) {
    headerDot.className = 'status-dot offline';
    headerText.textContent = 'Offline';
    return;
  }
  const running = !!data.running;

  headerDot.className = 'status-dot ' + (running ? 'running' : 'stopped');
  headerText.textContent = running ? 'Harvey is running' : 'Harvey is stopped';
  document.getElementById('control-dot').className = 'dot ' + (running ? 'running' : 'stopped');
  const label = document.getElementById('control-label');
  label.className = 'label ' + (running ? 'running' : 'stopped');
  label.textContent = running ? 'Running' : 'Stopped';

  const meta = document.getElementById('control-meta');
  if (running && data.pid) {
    let info = 'PID ' + escHtml(String(data.pid));
    if (data.started_at) info += ' &middot; started ' + formatDate(data.started_at);
    meta.innerHTML = info;
  } else {
    meta.innerHTML = 'Harvey wakes every few minutes, does what needs doing, and sleeps.';
  }

  document.getElementById('btn-start').style.display = running ? 'none' : '';
  document.getElementById('btn-stop').style.display = running ? '' : 'none';
}

async function startHarvey() {
  const btn = document.getElementById('btn-start');
  btn.disabled = true;
  const data = await api('/api/harvey/start', {method: 'POST'});
  if (data && data.success) showToast('Harvey started.', 'success');
  else showToast((data && data.message) || 'Failed to start.', 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function stopHarvey() {
  const btn = document.getElementById('btn-stop');
  btn.disabled = true;
  const data = await api('/api/harvey/stop', {method: 'POST'});
  if (data && data.success) showToast('Harvey stopped.', 'success');
  else showToast((data && data.message) || 'Failed to stop.', 'error');
  btn.disabled = false;
  loadHarveyStatus();
}

async function loadLogs() {
  const data = await api('/api/harvey/logs');
  const el = document.getElementById('log-viewer');
  if (data && data.lines && data.lines.length) {
    const stick = el.scrollTop + el.clientHeight >= el.scrollHeight - 30;
    el.textContent = data.lines.join('\n');
    if (stick) el.scrollTop = el.scrollHeight;
  } else {
    el.textContent = 'No logs yet. Start Harvey to see activity.';
  }
}

// ── Pipeline data ──

async function loadStats() {
  const grid = document.getElementById('stats-grid');
  const data = await api('/api/stats');
  if (!data) { grid.innerHTML = offlineState(); return; }
  if (data.error) {
    grid.innerHTML = emptyState('&#9670;', 'No pipeline data yet',
      'Start Harvey from the <b>Controls</b> tab and it will begin prospecting, writing, and sending on its own.');
    return;
  }
  const p = data.prospects || {}, c = data.campaigns || {}, v = data.conversations || {};
  const chips = (map) => {
    const entries = Object.entries(map || {});
    if (!entries.length) return '<span class="chip muted">none yet</span>';
    return entries.map(([k, n]) =>
      '<span class="chip">' + escHtml(k) + ' <b>' + escHtml(String(n)) + '</b></span>'
    ).join('');
  };
  const card = (label, value, breakdown) =>
    '<div class="stat-card"><div class="label">' + label + '</div>' +
    '<div class="value">' + value + '</div>' +
    '<div class="breakdown">' + breakdown + '</div></div>';

  grid.innerHTML =
    card('Prospects', p.total || 0, chips(p.by_status)) +
    card('Campaigns', c.total || 0, chips(c.by_status)) +
    card('Conversations', v.total || 0, chips(v.by_status)) +
    card('Actions Logged', data.actions_total || 0,
      '<span class="chip">Claude calls today <b>' + escHtml(String(data.claude_calls_today || 0)) + '</b></span>');
}

function fmtTokens(n) {
  n = n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(n);
}

function fmtCost(v) { return '$' + (v || 0).toFixed(2); }

function emailTag(p) {
  if (!p.email) return '';
  // Fall back to the legacy boolean for rows predating email_status.
  const status = p.email_status || (p.email_verified ? 'verified' : 'guess');
  const labels = {verified: 'verified', risky: 'catch-all', guess: 'guess', invalid: 'invalid'};
  if (!labels[status]) return '';
  return ' <span class="email-tag ' + status + '">' + labels[status] + '</span>';
}

async function loadUsage() {
  const data = await api('/api/usage');
  const statsEl = document.getElementById('usage-stats');
  if (!data) { statsEl.innerHTML = offlineState(); return; }

  // Quota gauges — the same numbers `/usage` shows in Claude Code.
  const quotaEl = document.getElementById('usage-quota');
  if (data.quota && Object.keys(data.quota).length) {
    const labels = {five_hour: '5-hour window', seven_day: 'Weekly'};
    let qHtml = '<h2>Claude Subscription Quota</h2>';
    for (const [key, w] of Object.entries(data.quota)) {
      const pct = Math.min(100, Math.max(0, w.utilization || 0));
      const color = pct >= 80 ? 'yellow' : 'green';
      const resets = w.resets_at ? 'resets ' + formatDate(w.resets_at) : '';
      qHtml += '<div class="progress-wrap">' +
        '<div class="progress-label">' +
          '<span class="text">' + escHtml(labels[key] || key) + (resets ? ' &middot; ' + escHtml(resets) : '') + '</span>' +
          '<span class="pct">' + pct.toFixed(0) + '%</span>' +
        '</div>' +
        '<div class="progress-bar"><div class="progress-fill ' + color + '" style="width:' + pct + '%"></div></div>' +
      '</div>';
    }
    quotaEl.innerHTML = qHtml;
    quotaEl.style.display = 'block';
  } else {
    quotaEl.style.display = 'none';
  }

  // Totals cards
  const t = data.totals || {};
  const card = (label, p) => {
    p = p || {};
    return '<div class="stat-card"><div class="label">' + label + '</div>' +
      '<div class="value">' + fmtCost(p.cost_usd) + '</div>' +
      '<div class="breakdown">' +
        '<span class="chip">calls <b>' + (p.calls || 0) + '</b></span>' +
        '<span class="chip">out <b>' + fmtTokens(p.output_tokens) + '</b></span>' +
        '<span class="chip">in <b>' + fmtTokens(p.input_tokens) + '</b></span>' +
        '<span class="chip">cached <b>' + fmtTokens(p.cache_read_tokens) + '</b></span>' +
      '</div></div>';
  };
  statsEl.innerHTML = card('Today', t.today) + card('Last 7 Days', t.week) + card('Last 30 Days', t.month);

  // Daily bars
  const dailyEl = document.getElementById('usage-daily');
  const days = data.by_day || [];
  if (days.length) {
    const maxCost = Math.max(...days.map(d => d.cost_usd || 0), 0.0001);
    let dHtml = '<h2>Daily Cost (equivalent API price, 30 days)</h2>';
    for (const d of days.slice(-30)) {
      const pct = Math.max(2, (d.cost_usd || 0) / maxCost * 100);
      dHtml += '<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px;font-size:12px">' +
        '<span class="muted" style="width:78px;flex-shrink:0;font-family:var(--mono)">' + escHtml(d.day || '') + '</span>' +
        '<div style="flex:1;background:rgba(255,255,255,0.05);border-radius:99px;height:10px;overflow:hidden">' +
          '<div style="width:' + pct + '%;height:100%;border-radius:99px;background:linear-gradient(90deg,var(--accent-deep),var(--accent))"></div>' +
        '</div>' +
        '<span style="width:120px;text-align:right;font-variant-numeric:tabular-nums">' + fmtCost(d.cost_usd) +
          ' <span class="muted">&middot; ' + (d.calls || 0) + ' calls</span></span>' +
      '</div>';
    }
    dailyEl.innerHTML = dHtml;
    dailyEl.style.display = 'block';
  } else {
    dailyEl.style.display = 'none';
  }

  // Breakdown tables
  const tablesEl = document.getElementById('usage-tables');
  const table = (title, rows, keyName) => {
    if (!rows || !rows.length) return '';
    let h = '<div class="card"><h2>' + title + '</h2><div class="table-card"><table><thead><tr>' +
      '<th>' + keyName + '</th><th>Calls</th><th>Input</th><th>Output</th><th>Cache read</th><th>Est. cost</th>' +
      '</tr></thead><tbody>';
    for (const r of rows) {
      h += '<tr><td>' + escHtml(String(r[keyName.toLowerCase()] || '')) + '</td>' +
        '<td>' + (r.calls || 0) + '</td>' +
        '<td class="muted">' + fmtTokens(r.input_tokens) + '</td>' +
        '<td>' + fmtTokens(r.output_tokens) + '</td>' +
        '<td class="muted">' + fmtTokens(r.cache_read_tokens) + '</td>' +
        '<td>' + fmtCost(r.cost_usd) + '</td></tr>';
    }
    return h + '</tbody></table></div></div>';
  };

  const anyRows = (data.by_agent || []).length || (data.by_task || []).length;
  if (!anyRows) {
    tablesEl.innerHTML = emptyState('&#9680;', 'No usage recorded yet',
      'Once Harvey starts making Claude calls, every one is logged here with exact tokens and equivalent API cost. Run <b>harvey usage --reconcile</b> to backfill from Claude Code transcripts.');
  } else {
    tablesEl.innerHTML =
      table('By Agent (30 days)', data.by_agent, 'Agent') +
      table('By Task (30 days)', data.by_task, 'Task') +
      table('By Model (30 days)', data.by_model, 'Model');
  }
}

async function loadCompanies() {
  companyDrill = false;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies');
  if (!data) { el.innerHTML = offlineState(); return; }
  _companies = data;
  if (!data.length) {
    el.innerHTML = emptyState('&#9906;', 'No companies yet',
      'Harvey\'s Scout agent hasn\'t researched any companies. Finish <b>Setup</b>, then start Harvey from the <b>Controls</b> tab.');
    return;
  }
  let html = '<div class="table-card"><table><thead><tr><th>Company</th><th>Domain</th><th>Industry</th><th>Size</th><th>Location</th><th>Contacts</th><th>Source</th><th>Added</th></tr></thead><tbody>';
  data.forEach((c, i) => {
    const website = c.website || (c.domain ? 'https://' + c.domain : '');
    const nameLink = website
      ? '<a href="' + escHtml(website) + '" target="_blank" rel="noopener" onclick="event.stopPropagation()">' + escHtml(c.name) + '</a>'
      : escHtml(c.name);
    html += '<tr style="cursor:pointer" onclick="showCompanyContacts(' + i + ')">' +
      '<td>' + nameLink + '</td><td class="muted">' + escHtml(c.domain) + '</td><td>' + escHtml(c.industry) + '</td>' +
      '<td>' + escHtml(c.company_size) + '</td><td>' + escHtml(c.location) + '</td>' +
      '<td>' + (c.contact_count || 0) + '</td><td class="muted">' + escHtml(c.source) + '</td>' +
      '<td class="muted">' + formatDate(c.created_at) + '</td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

async function showCompanyContacts(index) {
  const company = _companies[index];
  if (!company) return;
  companyDrill = true;
  const el = document.getElementById('companies-list');
  const data = await api('/api/companies/' + encodeURIComponent(company.id) + '/contacts');
  let html = '<div class="card"><h2>' + escHtml(company.name) + ' — Contacts</h2>' +
    '<button class="btn btn-secondary btn-sm" onclick="loadCompanies()" style="margin-bottom:16px">&larr; Back to Companies</button>';
  if (!data || !data.length) {
    html += '<p style="color:var(--text-3);font-size:13px">No contacts found at this company yet.</p></div>';
  } else {
    html += '<div class="table-card"><table><thead><tr><th>Name</th><th>Title</th><th>Email</th><th>Phone</th><th>LinkedIn</th><th>Status</th><th>Source</th></tr></thead><tbody>';
    for (const p of data) {
      const emailIcon = emailTag(p);
      const phoneIcon = p.phone_verified ? ' <span class="verified">&#10003;</span>' : '';
      html += '<tr><td>' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</td>' +
        '<td>' + escHtml(p.title) + '</td><td>' + escHtml(p.email) + emailIcon + '</td>' +
        '<td>' + escHtml(p.phone) + phoneIcon + '</td>' +
        '<td>' + (p.linkedin_url ? '<a href="' + escHtml(p.linkedin_url) + '" target="_blank" rel="noopener">Profile</a>' : '') + '</td>' +
        '<td>' + badge(p.status) + '</td><td class="muted">' + escHtml(p.source) + '</td></tr>';
    }
    html += '</tbody></table></div></div>';
  }
  el.innerHTML = html;
}

async function submitFeedback(entityType, entityId, promptText) {
  const comment = prompt(promptText || 'Add your feedback:');
  if (!comment) return;
  const data = await api('/api/feedback', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({entity_type: entityType, entity_id: entityId, comment: comment})
  });
  if (data && data.success) showToast('Feedback saved. Harvey will take it into account.', 'success');
  else showToast((data && data.message) || 'Could not save feedback.', 'error');
}

function fbProspect(i) {
  const p = _prospects[i];
  if (p) submitFeedback('contact', p.id, 'Feedback on this contact:');
}

function fbCampaign(i) {
  const c = _campaigns[i];
  if (c) submitFeedback('campaign', c.id, 'Leave feedback on this campaign:');
}

async function loadProspects() {
  const el = document.getElementById('prospects-table');
  const data = await api('/api/prospects');
  if (!data) { el.innerHTML = offlineState(); return; }
  _prospects = data;
  if (!data.length) {
    el.innerHTML = emptyState('&#9673;', 'No contacts yet',
      'Harvey hasn\'t found any prospects. Once it\'s running, the Scout agent searches the web for people matching your ideal customer profile in <b>harvey.yaml</b>.');
    return;
  }
  let html = '<div class="table-card"><table><thead><tr><th>Name</th><th>Title</th><th>Company</th><th>Email</th><th>Phone</th><th>Status</th><th>Source</th><th>Added</th><th></th></tr></thead><tbody>';
  data.forEach((p, i) => {
    const emailV = p.email ? (escHtml(p.email) + emailTag(p)) : '';
    const phoneV = p.phone ? (escHtml(p.phone) + (p.phone_verified ? ' <span class="verified">&#10003;</span>' : '')) : '';
    html += '<tr><td>' + escHtml(p.first_name) + ' ' + escHtml(p.last_name) + '</td>' +
      '<td>' + escHtml(p.title) + '</td><td>' + escHtml(p.company) + '</td>' +
      '<td>' + emailV + '</td><td>' + phoneV + '</td><td>' + badge(p.status) + '</td>' +
      '<td class="muted">' + escHtml(p.source) + '</td><td class="muted">' + formatDate(p.created_at) + '</td>' +
      '<td><button class="btn btn-secondary btn-sm" onclick="fbProspect(' + i + ')">Feedback</button></td></tr>';
  });
  el.innerHTML = html + '</tbody></table></div>';
}

async function loadCampaigns() {
  const el = document.getElementById('campaigns-list');
  const data = await api('/api/campaigns');
  if (!data) { el.innerHTML = offlineState(); return; }
  _campaigns = data;
  if (!data.length) {
    el.innerHTML = emptyState('&#9993;', 'No campaigns yet',
      'The Writer agent hasn\'t drafted any sequences. It kicks in automatically once Harvey has scored prospects to write for.');
    return;
  }
  let html = '';
  data.forEach((c, i) => {
    let stepsHtml = '';
    for (const step of (c.sequence || [])) {
      stepsHtml += '<div class="email-step"><div class="step-num">Email ' + escHtml(String(step.step || '?')) +
        (step.delay_days ? ' &middot; send after ' + escHtml(String(step.delay_days)) + ' days' : '') + '</div>' +
        '<div class="subject">' + escHtml(step.subject) + '</div>' +
        '<div class="body">' + escHtml(step.body) + '</div></div>';
    }
    const pc = (c.prospect_ids || []).length;
    html += '<div class="campaign-card"><h3>' + escHtml(c.name || 'Untitled Campaign') + '</h3>' +
      '<div class="meta">' + badge(c.status) + '<span>' + escHtml(c.channel || 'email') + '</span>' +
      '<span>' + pc + ' prospect' + (pc !== 1 ? 's' : '') + '</span><span>' + formatDate(c.created_at) + '</span>' +
      '<button class="btn btn-secondary btn-sm" onclick="fbCampaign(' + i + ')">Feedback</button></div>' +
      (stepsHtml || '<p style="color:var(--text-3);font-size:13px">No email steps in this campaign.</p>') + '</div>';
  });
  el.innerHTML = html;
}

async function loadConversations() {
  const el = document.getElementById('conversations-list');
  const data = await api('/api/conversations');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState('&#9737;', 'No conversations yet',
      'No prospects have replied so far. When they do, the Handler agent classifies each reply and responds — every thread shows up here.');
    return;
  }
  let html = '';
  for (const c of data) {
    let threadHtml = '';
    for (const msg of (c.thread || [])) {
      const cls = msg.sender === 'harvey' ? 'sent' : 'received';
      threadHtml += '<div class="thread-msg ' + cls + '"><div class="sender">' + escHtml(msg.sender) +
        ' &middot; ' + formatDate(msg.timestamp) + '</div>' + escHtml(msg.content) + '</div>';
    }
    const name = [c.first_name, c.last_name].filter(Boolean).join(' ') || 'Unknown';
    html += '<div class="convo-card"><h3>' + escHtml(name) +
      (c.company ? ' <span style="color:var(--text-3);font-weight:500">&mdash; ' + escHtml(c.company) + '</span>' : '') + '</h3>' +
      '<div class="meta">' + badge(c.status) + (c.intent ? badge(c.intent) : '') +
      '<span>' + escHtml(c.prospect_email || '') + '</span><span>' + formatDate(c.updated_at) + '</span></div>' +
      (threadHtml || '<p style="color:var(--text-3);font-size:13px">No messages in this thread yet.</p>') + '</div>';
  }
  el.innerHTML = html;
}

async function loadActivity() {
  const el = document.getElementById('activity-list');
  const data = await api('/api/activity');
  if (!data) { el.innerHTML = offlineState(); return; }
  if (!data.length) {
    el.innerHTML = emptyState('&#9202;', 'No activity yet',
      'Harvey hasn\'t taken any actions. Every prospect found, email written, and reply handled will appear here the moment it happens.');
    return;
  }
  let html = '<div class="activity-feed">';
  for (const a of data) {
    html += '<div class="activity-item"><span class="time">' + formatDate(a.created_at) + '</span>' +
      '<span class="agent">' + escHtml(a.agent) + '</span>' +
      '<span class="action">' + escHtml(a.action_type) + '</span></div>';
  }
  el.innerHTML = html + '</div>';
}

// ── Init & live refresh ──

loadSetupStatus();
loadHarveyStatus();

// Agent status: quick poll
setInterval(loadHarveyStatus, 8000);

// Data tabs: auto-refresh live stats without clobbering form input
setInterval(() => {
  if (document.hidden) return;
  switch (currentTab) {
    case 'setup': loadSetupStatus(); break;
    case 'overview': loadStats(); break;
    case 'companies': if (!companyDrill) loadCompanies(); break;
    case 'prospects': loadProspects(); break;
    case 'campaigns': loadCampaigns(); break;
    case 'conversations': loadConversations(); break;
    case 'activity': loadActivity(); break;
    case 'usage': loadUsage(); break;
    case 'controls': loadLogs(); break;
    // settings & help: never auto-refreshed (user may be typing)
  }
}, 15000);
</script>
</body>
</html>
"""


def start_dashboard(host: str = "127.0.0.1", port: int = 5555):
    """Start the dashboard server."""
    import uvicorn

    print(f"\n  Harvey Dashboard running at http://{host}:{port}")
    print("  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="warning")
