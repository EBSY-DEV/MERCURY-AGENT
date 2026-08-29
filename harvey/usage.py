"""Usage accounting — model pricing and Claude Code transcript reconciliation.

Harvey's primary usage source is the per-call result JSON from `claude -p
--output-format json` (recorded live by the Brain). This module supplies:

1. A pricing table + cost math for when the CLI doesn't report a cost
   (transcript rows have ``costUSD: null`` on Max subscriptions).
2. A reconciler that scans Claude Code's JSONL transcripts and backfills
   usage_events rows for calls the live path missed (crashes, other tools
   using Claude on this machine). Idempotent: rows are keyed by
   (message.id, requestId) with a unique index, and sessions already
   captured live are skipped entirely.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from harvey.state import StateManager

logger = logging.getLogger("harvey.usage")

# USD per million tokens: (input, output). Matched by longest prefix, so
# order here doesn't matter but specificity does — the dated Opus 4.0 id
# must not fall through to the modern "claude-opus-4" rate.
PRICING: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos": (10.0, 50.0),
    "claude-opus-4-2025": (15.0, 75.0),   # claude-opus-4-20250514 (Opus 4.0)
    "claude-opus-4-0": (15.0, 75.0),
    "claude-opus-4-1": (15.0, 75.0),
    "claude-opus-4": (5.0, 25.0),         # Opus 4.5 / 4.6 / 4.7 / 4.8
    "claude-sonnet": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-haiku": (0.25, 1.25),
}

# Cache pricing multipliers relative to base input price.
CACHE_READ_MULT = 0.1
CACHE_WRITE_5M_MULT = 1.25
CACHE_WRITE_1H_MULT = 2.0


def match_pricing(model: str) -> tuple[float, float] | None:
    """Longest-prefix match a model id to (input, output) $/MTok."""
    if not model:
        return None
    best_key = ""
    for key in PRICING:
        if model.startswith(key) and len(key) > len(best_key):
            best_key = key
    return PRICING[best_key] if best_key else None


def compute_cost(
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_5m_tokens: int = 0,
    cache_write_1h_tokens: int = 0,
) -> float:
    """Equivalent API-list-price cost in USD. 0.0 for unknown models."""
    prices = match_pricing(model)
    if not prices:
        return 0.0
    p_in, p_out = prices
    per_tok_in = p_in / 1_000_000
    per_tok_out = p_out / 1_000_000
    return (
        input_tokens * per_tok_in
        + output_tokens * per_tok_out
        + cache_read_tokens * per_tok_in * CACHE_READ_MULT
        + cache_write_5m_tokens * per_tok_in * CACHE_WRITE_5M_MULT
        + cache_write_1h_tokens * per_tok_in * CACHE_WRITE_1H_MULT
    )


def claude_config_dir() -> Path:
    """Claude Code's config directory (honors CLAUDE_CONFIG_DIR)."""
    override = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".claude"


def _iso_to_sqlite_utc(ts: str) -> str | None:
    """'2026-08-28T20:15:03.123Z' -> '2026-08-28 20:15:03' (UTC, naive)."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def parse_transcript_events(path: Path) -> dict[str, dict]:
    """Extract per-request usage from one session transcript.

    Returns {request_key: event_dict}. Claude Code writes multiple
    entries per API request (streaming snapshots, resume replays) — the
    LAST entry per (message.id, requestId) is authoritative, so later
    lines simply overwrite earlier ones here.
    """
    events: dict[str, dict] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "assistant":
                    continue
                message = entry.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue

                msg_id = str(message.get("id") or "")
                req_id = str(entry.get("requestId") or "")
                if not msg_id and not req_id:
                    continue
                key = f"{msg_id}:{req_id}"

                model = str(message.get("model") or "")
                input_tokens = int(usage.get("input_tokens") or 0)
                output_tokens = int(usage.get("output_tokens") or 0)
                cache_read = int(usage.get("cache_read_input_tokens") or 0)
                cache_creation = int(usage.get("cache_creation_input_tokens") or 0)

                # 5m/1h split (when present) prices cache writes correctly.
                split = usage.get("cache_creation")
                if isinstance(split, dict):
                    write_1h = int(split.get("ephemeral_1h_input_tokens") or 0)
                    write_5m = int(split.get("ephemeral_5m_input_tokens") or 0)
                else:
                    write_1h, write_5m = 0, cache_creation

                cost = entry.get("costUSD")
                if not isinstance(cost, (int, float)):
                    cost = compute_cost(
                        model,
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        cache_read_tokens=cache_read,
                        cache_write_5m_tokens=write_5m,
                        cache_write_1h_tokens=write_1h,
                    )

                events[key] = {
                    "request_key": key,
                    "session_id": str(entry.get("sessionId") or ""),
                    "model": model,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cache_read_tokens": cache_read,
                    "cache_creation_tokens": cache_creation,
                    "cost_usd": float(cost),
                    "created_at": _iso_to_sqlite_utc(str(entry.get("timestamp") or "")),
                }
    except OSError as e:
        logger.debug(f"Could not read transcript {path}: {e}")
    return events


async def reconcile_transcripts(
    state: StateManager,
    since_days: int = 7,
    config_dir: Path | None = None,
) -> int:
    """Backfill usage_events from Claude Code JSONL transcripts.

    Only touches transcripts modified in the last ``since_days`` days,
    skips sessions Harvey already recorded live, and relies on the
    request_key unique index for idempotency. Returns rows inserted.
    """
    base = (config_dir or claude_config_dir()) / "projects"
    if not base.is_dir():
        logger.debug(f"No Claude transcripts directory at {base}")
        return 0

    try:
        recorded_sessions = await state.get_recorded_session_ids()
    except Exception as e:
        logger.warning(f"Could not load recorded sessions: {e}")
        recorded_sessions = set()

    cutoff = datetime.now().timestamp() - since_days * 86400
    inserted = 0

    for path in base.glob("*/*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                continue
        except OSError:
            continue

        for event in parse_transcript_events(path).values():
            if event["session_id"] and event["session_id"] in recorded_sessions:
                continue
            try:
                if await state.record_usage_event(source="transcript", **event):
                    inserted += 1
            except Exception as e:
                logger.debug(f"Failed to record transcript event: {e}")

    if inserted:
        logger.info(f"Usage reconcile: backfilled {inserted} transcript event(s).")
    return inserted
