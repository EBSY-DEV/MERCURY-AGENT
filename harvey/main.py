"""Harvey's main heartbeat loop. Always Be Closing."""

import asyncio
import logging
import signal
import sys
from datetime import datetime, time, timedelta
from typing import NamedTuple

import pytz

from harvey.brain import Brain
from harvey.config import ConfigError, load_config, load_env, HarveyConfig
from harvey.pipeline import run_profile_stage
from harvey.state import StateManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("harvey")

# Backoff for consecutive failed cycles: 60s, 120s, 240s, ... capped at 15 min
ERROR_BACKOFF_BASE = 60
ERROR_BACKOFF_CAP = 900


def in_quiet_hours(config: HarveyConfig) -> bool:
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


def seconds_until_quiet_hours_end(config: HarveyConfig) -> int:
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
    config: HarveyConfig,
    summary: dict | None = None,
) -> str:
    """Decide what Harvey should do next based on current state.

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

    if open_conversations > 0:
        action, reason = "handle_replies", f"{open_conversations} open conversation(s) waiting"
    elif draft_campaigns > 0:
        action, reason = "send_campaign", f"{draft_campaigns} draft campaign(s) ready to deploy"
    elif new_prospects > 0:
        action, reason = "write_campaign", f"{new_prospects} new prospect(s) with no drafts ready"
    elif new_prospects < 20:
        action, reason = "prospect", f"only {new_prospects} new prospect(s); pipeline needs leads"
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

    config: HarveyConfig
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
    from harvey.agents.scout import Scout
    from harvey.agents.writer import Writer
    from harvey.agents.sender import Sender
    from harvey.agents.handler import Handler
    from harvey.agents.analyst import Analyst

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

    Returns the action taken, or ``budget_exhausted`` when Harvey declined
    to spend any more of today's Claude quota.
    """
    config = rt.config
    max_calls = max(int(200 * (config.usage.max_daily_claude_percent / 100)), 1)

    # 1. Check usage budget (real subscription quota when readable, else
    # Harvey's own call counter)
    if not await rt.brain.is_within_budget(
        max_calls, max_percent=config.usage.max_daily_claude_percent
    ):
        logger.info(
            f"Claude usage limit reached "
            f"({config.usage.max_daily_claude_percent}% of quota or "
            f"{max_calls} calls)."
        )
        return "budget_exhausted"

    # 2. Decide what to do
    logger.info("Checking pipeline state...")
    summary = await rt.state.get_state_summary()
    action = await decide_next_action(rt.brain, rt.state, config, summary=summary)

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

    # Profiling rides along every cycle. It is three HTTP requests per
    # business with no model call, so it costs nothing against the
    # Claude budget the rest of this loop is rationing -- and it is what
    # turns a name and a domain into something worth writing about.
    if summary.get("unprofiled", 0):
        tasks.append(("profile", run_profile_stage(rt.state, limit=25)))

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
    """Harvey's main loop. Wakes up, decides, acts, sleeps. Repeat."""
    if stop_event is None:
        stop_event = asyncio.Event()

    logger.info("=" * 60)
    logger.info("Harvey is online. Always Be Closing.")
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

            if action == "budget_exhausted":
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

    logger.info("Harvey shutting down. Deals don't close themselves, but I need a break.")


def _has_credentials() -> bool:
    """True when Harvey has credentials from *somewhere*.

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
    return any(
        str(v).strip()
        for k, v in values.items()
        if k not in ("smtp_port", "imap_port")
    )


def _needs_setup() -> bool:
    """Check if Harvey needs first-time setup."""
    from pathlib import Path
    from harvey.config import _find_config_file
    from harvey.paths import PROJECT_ROOT
    project_root = PROJECT_ROOT
    env_file = project_root / ".env"

    # No credentials in a file *or* the environment: definitely needs setup
    if not env_file.exists() and not _has_credentials():
        return True

    # Resolve the config the same way the rest of Harvey does, rather than
    # hardcoding harvey.yaml: harvey.local.yaml wins when present, and that
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
    """``harvey run --once`` entry point: one cycle, no wizard, no loop.

    Never falls into the interactive setup wizard -- a scheduled run has no
    terminal to answer it -- and returns an exit code instead, so a cron
    entry or a CI job can tell a bad configuration from a quiet cycle.
    """
    if _needs_setup():
        logger.error(
            "Harvey is not configured: no credentials found, or harvey.yaml "
            "is still the template. Run 'harvey setup' or 'harvey train <url>'."
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
        print("\n  First time running Harvey? Let's get you set up.\n")
        from harvey.setup import run_setup
        asyncio.run(run_setup())
        return

    try:
        asyncio.run(_run_with_signals())
    except KeyboardInterrupt:
        logger.info("Goodbye.")


if __name__ == "__main__":
    main()
