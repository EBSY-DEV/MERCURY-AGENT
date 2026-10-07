"""Mercury's main heartbeat loop. Always Be Closing."""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time, timedelta
from typing import NamedTuple

import pytz

from mercury.brain import Brain
from mercury.config import ConfigError, load_config, load_env, MercuryConfig
from mercury.pipeline import run_profile_stage
from mercury.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# httpx logs every request URL at INFO. Reoon and other verifiers take the
# API key as a query parameter, so that line would write secrets into the
# journal. Warnings and errors still come through.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("mercury")

# Listings discovery (Google Maps, OSM, DataForSEO) is a daily background
# job: one full pass is dozens of scraper jobs of a few minutes each, far
# longer than a heartbeat, so it must never block a cycle.
DISCOVERY_EVERY_HOURS = 24

# What run_cycle returns when today's Claude quota is spent. The loop checks
# for exactly this value to back off for an hour instead of re-running a
# full cycle every heartbeat.
OVER_BUDGET = "over_budget"
DISCOVERY_LIMIT_PER_QUERY = 40
_discovery_task: asyncio.Task | None = None


async def _maybe_start_discovery(rt) -> None:
    """Kick off the daily discovery run when it is due; return at once."""
    global _discovery_task
    if _discovery_task and not _discovery_task.done():
        return
    provider = await rt.state.get_setting("discovery_provider")
    if not provider:
        return
    now = datetime.now(pytz.utc)
    last = await rt.state.get_setting("discovery_last_auto_run") or ""
    if last:
        try:
            if now - datetime.fromisoformat(last) < timedelta(hours=DISCOVERY_EVERY_HOURS):
                return
        except ValueError:
            pass

    from mercury.collectors.discover import PROVIDERS, build_queries
    from mercury.pipeline import run_prospecting

    if provider not in PROVIDERS:
        logger.warning(f"Discovery: unknown provider {provider!r} selected; skipping.")
        return
    queries = build_queries(rt.config, limit=DISCOVERY_LIMIT_PER_QUERY)
    if not queries:
        return
    await rt.state.set_setting("discovery_last_auto_run", now.isoformat())
    # A dashboard "stop" only ends the run it interrupted.
    await rt.state.set_setting("discovery_paused", "")
    logger.info(
        f"Discovery: daily background run via {provider} ({len(queries)} queries)."
    )

    async def _go():
        try:
            result = await run_prospecting(
                rt.state, rt.config, provider, queries, max_spend=1.0
            )
            d = result.discover or {}
            logger.info(
                f"Discovery: {provider} done — found {d.get('found')}, "
                f"new {d.get('new_companies')}, known {d.get('known_companies')}, "
                f"junk {d.get('junk')}, profiled {result.profiled_companies}, "
                f"errors {len(result.errors)}."
            )
            for err in result.errors[:5]:
                logger.warning(f"Discovery: {err}")
        except Exception:
            logger.exception("Discovery: background run failed")

    _discovery_task = asyncio.create_task(_go())


# Backoff for consecutive failed cycles: 60s, 120s, 240s, ... capped at 15 min
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900


def in_quiet_hours(config: MercuryConfig) -> bool:
    """Check if we're currently in quiet hours."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz).time()
    start = time.fromisoformat(qh.start)
    end = time.fromisoformat(qh.end)

    if start <= end:
        return start <= now <= end
    else:
        # Quiet hours cross midnight (e.g., 22:00 - 07:00)
        return now >= start or now <= end


def seconds_until_quiet_hours_end(config: MercuryConfig) -> int:
    """Calculate seconds until quiet hours end."""
    qh = config.usage.quiet_hours
    tz = pytz.timezone(qh.timezone)
    now = datetime.now(tz)
    end = time.fromisoformat(qh.end)
    end_today = now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)

    if end_today <= now:
        # End time is tomorrow
        end_today = end_today + timedelta(days=1)

    delta = end_today - now
    return max(int(delta.total_seconds()), 60)


async def decide_next_action(
    brain: Brain,
    state: StateManager,
    config: MercuryConfig,
    summary: dict | None = None,
) -> str:
    """Decide what Mercury should do next based on current state.

    Uses deterministic priority rules (handle_replies > send_campaign >
    write_campaign > prospect > idle) instead of burning a Claude call on a
    decision that is fully derivable from pipeline counts. This saves budget
    every cycle and removes a fragile LLM string-parsing step.
    """
    if summary is None:
        summary = await state.get_state_summary()

    prospects = summary.get("prospects") or {}
    new_prospects = prospects.get("new", 0) if isinstance(prospects, dict) else 0
    draft_campaigns = summary.get("draft_campaigns", 0) or 0
    open_conversations = summary.get("open_conversations", 0) or 0

    # A draft that cannot be deployed must not outrank everything below it.
    # With sending paused (or every draft waiting on a human), send_campaign
    # wins the priority chain forever and Mercury stops prospecting and writing
    # entirely: one unreviewed email freezes the whole agent.
    sending_paused = False
    try:
        from mercury.agents.sender import KILL_SWITCH_KEY

        sending_paused = bool(await state.get_setting(KILL_SWITCH_KEY))
    except Exception:  # pragma: no cover - never block a cycle on this
        sending_paused = False

    # Prospects the Writer cannot act on must not count as pending work.
    # A handful of contacts with no deliverable address kept write_campaign
    # winning every cycle while the Writer did nothing, so the loop never
    # reached prospecting and the agent quietly stopped finding anyone.
    # Only what the Sender could actually send is worth drafting: with
    # send_to_risky off, a 'risky' prospect was drafted (Claude calls) and
    # then sat unsendable forever.
    allow_risky_write = bool(getattr(config.channels.email, "send_to_risky", False))
    writable_statuses = "'verified', 'risky'" if allow_risky_write else "'verified'"
    writable_prospects = new_prospects
    if new_prospects:
        try:
            import aiosqlite

            async with aiosqlite.connect(state.db_path) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM prospects WHERE status = 'new' "
                    f"AND email != '' AND email_status IN ({writable_statuses})"
                ) as cursor:
                    writable_prospects = (await cursor.fetchone())[0]
        except Exception:  # pragma: no cover - never block a cycle on this
            writable_prospects = new_prospects

    # Drafting costs Claude calls, and with a 5/day cap a draft written today
    # goes out in two weeks, by which time the facts and the copy rules have
    # moved on. Stop drafting once the queue already holds a week of sends.
    # With mailbox rotation the real ceiling is the mailboxes' (warming) caps.
    try:
        from mercury.integrations.mailboxes import planned_daily_capacity

        daily_capacity = planned_daily_capacity(config)
    except Exception:  # pragma: no cover - never block a cycle on this
        daily_capacity = int(getattr(config.channels.email, "max_daily_sends", 5) or 0)
    max_daily = max(int(daily_capacity or 1), 1)
    queued_first = 0
    if writable_prospects:
        try:
            import aiosqlite

            async with aiosqlite.connect(state.db_path) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM outbox WHERE step = 1 "
                    "AND status IN ('approved', 'pending_review')"
                ) as cursor:
                    queued_first = (await cursor.fetchone())[0]
        except Exception:  # pragma: no cover - never block a cycle on this
            queued_first = 0
        if queued_first >= 7 * max_daily:
            logger.info(
                f"Writer backlog: {queued_first} first emails queued "
                f"(~{queued_first // max_daily} days at {max_daily}/day); not drafting more."
            )
            writable_prospects = 0

    # "Draft campaigns exist" is not the same as "there is something to send".
    # With require_approval on, drafts sit waiting for a human, and counting
    # them made send_campaign win every cycle while nothing could actually go
    # out, so the agent stopped writing and prospecting. Count only mail the
    # Sender would really accept: approved, unsent, and to an address that
    # passes the deliverability gate.
    deployable = 0
    if draft_campaigns and not sending_paused:
        allow_risky = bool(getattr(config.channels.email, "send_to_risky", False))
        statuses = ("verified", "risky") if allow_risky else ("verified",)
        placeholders = ", ".join("?" for _ in statuses)
        try:
            import aiosqlite

            async with aiosqlite.connect(state.db_path) as db:
                async with db.execute(
                    "SELECT COUNT(*) FROM outbox o JOIN prospects p "
                    "ON p.email = o.to_email WHERE o.status = 'approved' "
                    "AND o.sent_at IS NULL "
                    # Mail held by an out-of-office pause is not sendable work.
                    "AND NOT (o.kind = 'sequence' AND EXISTS ("
                    "SELECT 1 FROM sequence_pauses sp WHERE sp.prospect_id = o.prospect_id "
                    "AND sp.state IN ('paused', 'needs_review'))) "
                    f"AND p.email_status IN ({placeholders})",
                    statuses,
                ) as cursor:
                    deployable = (await cursor.fetchone())[0]
        except Exception:  # pragma: no cover - never block a cycle on this
            deployable = draft_campaigns

    # With the daily cap reached nothing can leave for hours; letting
    # send_campaign win anyway idled the writer every other cycle while
    # verified prospects waited. The send_outbox ride-along still stages and
    # drains the moment the cap frees up.
    if deployable:
        try:
            if await state.count_outbox_sent_today() >= daily_capacity:
                deployable = 0
        except Exception:  # pragma: no cover - never block a cycle on this
            pass

    # Replies are handled by a ride-along every cycle (see run_cycle); an
    # open conversation must not outrank writing and prospecting forever.
    if deployable > 0:
        action, reason = "send_campaign", f"{deployable} approved email(s) ready to send"
    elif writable_prospects > 0:
        action, reason = "write_campaign", f"{writable_prospects} writable prospect(s) with no drafts ready"
    elif writable_prospects < 20:
        # Gate on what the Writer can use: twenty undeliverable 'new' rows
        # used to stop prospecting permanently.
        action, reason = "prospect", f"only {writable_prospects} writable prospect(s); pipeline needs leads"
    else:
        action, reason = "idle", "pipeline is healthy; running analysis"

    logger.info(f"Decision: {action} ({reason})")
    return action


async def _interruptible_sleep(seconds: float, stop_event: asyncio.Event) -> bool:
    """Sleep up to `seconds`, waking immediately on shutdown.

    Returns True if a shutdown was requested during the sleep.
    """
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


class Runtime(NamedTuple):
    """Everything a heartbeat cycle needs, constructed once per process."""

    config: MercuryConfig
    env: object
    state: StateManager
    brain: Brain
    scout: object
    writer: object
    sender: object
    handler: object
    analyst: object


async def build_runtime() -> Runtime | None:
    """Load config, open the database, and wire up the agents.

    Returns None when the configuration is unusable. Both entry points --
    the long-running loop and the one-shot scheduled run -- go through
    here, so they can never drift apart on how an agent is constructed.
    """
    try:
        config = load_config()
    except (ConfigError, Exception) as e:
        if isinstance(e, (KeyboardInterrupt, asyncio.CancelledError)):
            raise
        logger.error(f"Cannot start \u2014 configuration error:\n{e}")
        return None

    env = load_env()
    state = StateManager()
    brain = Brain(state)

    await state.init_db()
    logger.info("Database initialized.")

    # Import agents here to avoid circular imports
    from mercury.agents.scout import Scout
    from mercury.agents.writer import Writer
    from mercury.agents.sender import Sender
    from mercury.agents.handler import Handler
    from mercury.agents.analyst import Analyst

    return Runtime(
        config=config,
        env=env,
        state=state,
        brain=brain,
        scout=Scout(brain, state, config, env),
        writer=Writer(brain, state, config, env),
        sender=Sender(brain, state, config, env),
        handler=Handler(brain, state, config, env),
        analyst=Analyst(state),
    )


async def run_cycle(rt: Runtime) -> str:
    """One heartbeat: check the budget, decide, act, log the outcome.

    Quiet hours are deliberately the caller's business. The loop sleeps
    through them; a scheduled one-shot run is paced by whatever scheduler
    woke it and only needs to report the skip.

    Returns the action taken, or ``over_budget`` when today's Claude quota
    is spent: model work is skipped, the zero-Claude ride-alongs still run.
    """
    config = rt.config
    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)

    # 0. Re-queue outbox rows a previous process left in 'sending' (killed
    # mid-send). One UPDATE, no model call; cheap enough to run every cycle.
    try:
        recovered = await rt.state.recover_stale_outbox()
        if recovered:
            logger.warning(
                f"Recovered {recovered} outbox row(s) stuck in 'sending' from an "
                "interrupted send; they are approved again and will retry."
            )
    except Exception as e:
        logger.warning(f"Could not recover stale outbox rows: {e}")

    # 1. Check usage budget (real subscription quota when readable, else
    # Mercury's own call counter)
    over_budget = not await rt.brain.is_within_budget(
        max_calls, max_percent=config.usage.max_daily_claude_percent
    )
    if over_budget:
        logger.info(
            f"Claude usage limit reached "
            f"({config.usage.max_daily_claude_percent}% of quota or "
            f"{max_calls} calls). Skipping model work; the zero-Claude tasks "
            f"(outbox drain, inbox sweep, profiling, discovery) still run."
        )

    # 2. Decide what to do
    logger.info("Checking pipeline state...")
    summary = await rt.state.get_state_summary()
    action = await decide_next_action(rt.brain, rt.state, config, summary=summary)
    if over_budget and action != "send_campaign":
        # Every other primary action spends Claude calls (writer, scout
        # scoring, reply classification). Returning early here used to skip
        # the ride-alongs too, so a spent quota froze sending and sweeping
        # for the rest of the day.
        action = OVER_BUDGET

    # 3. Execute -- run independent agents in parallel where possible
    # Handler is always safe to run alongside other agents
    tasks = []
    has_open_convos = summary.get("open_conversations", 0) > 0

    if action == "handle_replies":
        tasks.append(("handle_replies", rt.handler.run()))
    elif action == "prospect":
        tasks.append(("prospect", rt.scout.run()))
        # Also handle replies in parallel if needed
        if has_open_convos:
            tasks.append(("handle_replies", rt.handler.run()))
        # Analyst is cheap (no Claude calls) -- keep analytics fresh
        tasks.append(("analyze", rt.analyst.run()))
    elif action == "write_campaign":
        tasks.append(("write_campaign", rt.writer.run()))
        if has_open_convos:
            tasks.append(("handle_replies", rt.handler.run()))
    elif action == "send_campaign":
        tasks.append(("send_campaign", rt.sender.run()))
    elif action == "idle":
        tasks.append(("analyze", rt.analyst.run()))

    # Native mail providers drain the outbox every cycle -- due sends
    # and approved replies must go out on schedule regardless of the
    # cycle's primary action.
    if rt.sender.is_native and not any(n == "send_campaign" for n, _ in tasks):
        tasks.append(("send_outbox", rt.sender.run()))

    # The inbox is polled every cycle on the native path, like the outbox
    # drain: one IMAP fetch, no model call unless a human actually replied.
    # Gated on the open-conversation count it never ran at all, because only
    # the handler opens conversations. Classification spends Claude, so it
    # waits while over budget.
    if (rt.handler.is_native and not over_budget
            and not any(n == "handle_replies" for n, _ in tasks)):
        tasks.append(("handle_replies", rt.handler.run()))

    # Profiling rides along every cycle. It is three HTTP requests per
    # business with no model call, so it costs nothing against the
    # Claude budget the rest of this loop is rationing -- and it is what
    # turns a name and a domain into something worth writing about.
    if summary.get("unprofiled", 0):
        tasks.append(("profile", run_profile_stage(rt.state, limit=25)))

    # The inbox sweep rides along every cycle for the same reason profiling
    # does. It used to live inside the "prospect" action, and that action is
    # only chosen when there is nothing to write or send, which with sending
    # enabled is never: discovered companies piled up with a domain and no
    # contact while the loop kept re-deciding "write". Each sweep is a few
    # HTTP requests plus one Reoon check per company, no model call.
    tasks.append(("sweep_inboxes", rt.scout._prospects_from_known_companies()))

    await _maybe_start_discovery(rt)

    if len(tasks) > 1:
        logger.info(f"Running {len(tasks)} agents in parallel: {[t[0] for t in tasks]}")

    # Run all tasks, catch errors per-task so one bad agent
    # never takes down the cycle
    results = await asyncio.gather(*[t[1] for t in tasks], return_exceptions=True)
    for (name, _), result in zip(tasks, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, Exception):
            logger.error(f"Agent {name} failed: {result}", exc_info=result)

    # 4. Log the action (best-effort; never kills the cycle)
    try:
        await rt.state.log_action(action_type=action, agent="main")
    except Exception as e:
        logger.warning(f"Failed to log action '{action}': {e}")

    return action


async def run_once(ignore_quiet_hours: bool = False) -> int:
    """Run exactly one cycle, then return a process exit code.

    This is the entry point for scheduled runs -- cron, a container job, a
    Claude Code Routine -- where something else owns the cadence and the
    process is expected to terminate. Quiet hours still apply unless the
    caller overrides them, so a schedule that overlaps them stays honest.
    """
    rt = await build_runtime()
    if rt is None:
        return 1

    if in_quiet_hours(rt.config):
        if not ignore_quiet_hours:
            logger.info("Quiet hours \u2014 skipping this run.")
            return 0
        logger.info("Quiet hours \u2014 running anyway (--ignore-quiet-hours).")

    try:
        action = await run_cycle(rt)
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as e:
        logger.error(f"Cycle failed: {e}", exc_info=True)
        return 1

    logger.info(f"Cycle complete: {action}")
    return 0


async def heartbeat(stop_event: asyncio.Event | None = None):
    """Mercury's main loop. Wakes up, decides, acts, sleeps. Repeat."""
    if stop_event is None:
        stop_event = asyncio.Event()

    logger.info("=" * 60)
    logger.info("Mercury Agent is online.")
    logger.info("=" * 60)

    rt = await build_runtime()
    if rt is None:
        return

    interval = rt.config.usage.heartbeat_interval_minutes * 60
    consecutive_errors = 0

    while not stop_event.is_set():
        try:
            # Quiet hours: sleep until they lift rather than burning a cycle
            if in_quiet_hours(rt.config):
                sleep_for = seconds_until_quiet_hours_end(rt.config)
                logger.info(f"Quiet hours. Sleeping for {sleep_for // 60} minutes.")
                if await _interruptible_sleep(sleep_for, stop_event):
                    break
                continue

            action = await run_cycle(rt)

            if action == OVER_BUDGET:
                logger.info("Sleeping 1h, then re-checking.")
                if await _interruptible_sleep(3600, stop_event):
                    break
                continue

            consecutive_errors = 0

            logger.info(
                f"Cycle complete. Sleeping for "
                f"{rt.config.usage.heartbeat_interval_minutes} minutes."
            )
            if await _interruptible_sleep(interval, stop_event):
                break

        except (KeyboardInterrupt, asyncio.CancelledError):
            break
        except Exception as e:
            consecutive_errors += 1
            backoff = min(
                ERROR_BACKOFF_BASE * (2 ** (consecutive_errors - 1)),
                ERROR_BACKOFF_CAP,
            )
            logger.error(
                f"Error in heartbeat (failure #{consecutive_errors}): {e}",
                exc_info=True,
            )
            logger.info(f"Recovering... sleeping {backoff}s before retry.")
            if await _interruptible_sleep(backoff, stop_event):
                break

    logger.info("Mercury shutting down. Deals don't close themselves, but I need a break.")


def _has_credentials() -> bool:
    """True when Mercury has credentials from *somewhere*.

    A ``.env`` file is the local convention, but a container or a scheduled
    cloud run gets the same values injected as real environment variables
    and has no file at all. So ask the loaded config, not the filesystem --
    otherwise a perfectly configured deployment looks unconfigured and
    drops into the interactive wizard with nobody there to answer it.
    """
    try:
        values = load_env().model_dump()
    except Exception:
        return False
    # Ports carry non-empty defaults, so they say nothing about setup.
    def _present(v) -> bool:
        if isinstance(v, dict):  # mailbox_secrets: "{}" is not a credential
            return any(str(x).strip() for x in v.values())
        return bool(str(v).strip())

    return any(
        _present(v)
        for k, v in values.items()
        if k not in ("smtp_port", "imap_port")
    )


def _needs_setup() -> bool:
    """Check if Mercury needs first-time setup."""
    from pathlib import Path
    from mercury.config import _find_config_file
    from mercury.paths import PROJECT_ROOT
    project_root = PROJECT_ROOT
    env_file = project_root / ".env"

    # No credentials in a file *or* the environment: definitely needs setup
    if not env_file.exists() and not _has_credentials():
        return True

    # Resolve the config the same way the rest of Mercury does, rather than
    # hardcoding mercury.yaml: mercury.local.yaml wins when present, and that
    # is exactly how a public checkout carries a trained, private
    # configuration. Checking the tracked template instead would declare a
    # perfectly configured deployment unconfigured.
    try:
        config_file = Path(_find_config_file())
    except Exception:
        return True

    try:
        with open(config_file) as f:
            import yaml
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            return True
        company = (config.get("persona") or {}).get("company", "")
        if company in ("Your Company", ""):
            return True
    except Exception:
        return True

    return False


async def _run_with_signals():
    """Run the heartbeat with SIGINT/SIGTERM wired to a graceful shutdown."""
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _request_shutdown(sig_name: str):
        if stop_event.is_set():
            logger.info("Second shutdown signal — exiting immediately.")
            sys.exit(1)
        logger.info(f"Received {sig_name}. Finishing current work, then shutting down...")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_shutdown, sig.name)
        except (NotImplementedError, RuntimeError):
            # Windows / non-main-thread fallback
            signal.signal(sig, lambda s, f: _request_shutdown(signal.Signals(s).name))

    await heartbeat(stop_event)


def run_once_main(ignore_quiet_hours: bool = False) -> int:
    """``mercury run --once`` entry point: one cycle, no wizard, no loop.

    Never falls into the interactive setup wizard -- a scheduled run has no
    terminal to answer it -- and returns an exit code instead, so a cron
    entry or a CI job can tell a bad configuration from a quiet cycle.
    """
    if _needs_setup():
        logger.error(
            "Mercury is not configured: no credentials found, or mercury.yaml "
            "is still the template. Run 'mercury setup' or 'mercury train <url>'."
        )
        return 1

    try:
        return asyncio.run(run_once(ignore_quiet_hours=ignore_quiet_hours))
    except KeyboardInterrupt:
        logger.info("Interrupted.")
        return 130


def main():
    """Entry point."""
    # Check for first-time setup
    if _needs_setup():
        print("\n  First time running Mercury? Let's get you set up.\n")
        from mercury.setup import run_setup
        asyncio.run(run_setup())
        return

    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
