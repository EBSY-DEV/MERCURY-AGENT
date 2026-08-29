"""Harvey CLI — simple commands to install, setup, run, and manage Harvey."""

import argparse
import asyncio
import subprocess
import sys
from pathlib import Path


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    print("\n  Installing Harvey dependencies...\n")

    # Install Python packages
    print("  [1/2] Installing Python packages...")
    requirements = Path(args._project_root) / "requirements.txt"
    if requirements.exists():
        pip_args = ["-r", "requirements.txt"]
    else:
        # Fall back to an editable install from pyproject.toml
        pip_args = ["-e", "."]
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", *pip_args],
        cwd=args._project_root,
    )
    if result.returncode != 0:
        print("\n  Failed to install Python packages.")
        print("  Tip: make sure you're inside a virtualenv "
              "(python3 -m venv .venv && source .venv/bin/activate).")
        sys.exit(1)
    print("  ✓ Python packages installed.\n")

    # Install Playwright browsers
    print("  [2/2] Installing Playwright browsers...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            timeout=600,
        )
        playwright_ok = result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        playwright_ok = False
    if not playwright_ok:
        print("\n  Playwright browser install failed (optional — needed for LinkedIn).")
    else:
        print("  ✓ Playwright browsers installed.\n")

    _ensure_importable(args._project_root)
    print("  Harvey is installed. Run 'harvey setup' next.\n")


def _ensure_importable(project_root: str):
    """Make sure `import harvey` works outside the repo directory.

    Python 3.13 silently skips .pth files carrying the macOS 'hidden'
    file flag, and some Macs propagate that flag to everything inside
    dot-directories like .venv — which breaks editable installs. When
    that happens, fall back to symlinking the package into site-packages
    (imports don't check the hidden flag; only .pth parsing does).
    """
    check = subprocess.run(
        [sys.executable, "-c", "import harvey"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return

    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "harvey"
        target = Path(project_root) / "harvey"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import harvey"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")

    print("\n  Warning: 'import harvey' fails outside the project directory.")
    print("  Run harvey commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from harvey.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Harvey's heartbeat loop."""
    from harvey.main import main

    main()


def cmd_train(args):
    """Train Harvey on a website."""
    from harvey.trainer import Trainer

    url = args.url.strip()
    if not url.startswith(("http://", "https://")):
        url = f"https://{url}"
        print(f"  No scheme given — using {url}")

    if args.max_pages < 1:
        print("  max_pages must be at least 1.")
        sys.exit(2)

    trainer = Trainer()
    asyncio.run(trainer.train(url, max_pages=args.max_pages))


def cmd_dashboard(args):
    """Launch the local web dashboard."""
    from harvey.dashboard import start_dashboard

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    start_dashboard(host=args.host, port=args.port)


def cmd_status(args):
    """Show current pipeline status."""
    from harvey.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Harvey Pipeline Status")
        print("  " + "=" * 40)
        print(f"  Prospects:            {summary['prospects']}")
        print(f"  Draft campaigns:      {summary['draft_campaigns']}")
        print(f"  Active campaigns:     {summary['active_campaigns']}")
        print(f"  Open conversations:   {summary['open_conversations']}")
        print(f"  Claude calls today:   {summary['usage_today']}")
        print()

    asyncio.run(_status())


def cmd_usage(args):
    """Show Claude usage: quota gauges, totals, per-agent breakdown."""
    from harvey.state import StateManager

    async def _usage():
        state = StateManager()
        await state.init_db()

        if args.reconcile:
            from harvey.usage import reconcile_transcripts
            inserted = await reconcile_transcripts(state, since_days=args.days)
            print(f"\n  Reconciled transcripts: {inserted} event(s) backfilled.")

        # Live quota (best-effort; undocumented endpoint)
        from harvey.integrations.quota import QuotaClient
        windows = None
        try:
            windows = await QuotaClient().get_utilization()
        except Exception:
            pass

        print("\n  Claude Usage")
        print("  " + "=" * 52)
        if windows:
            labels = {"five_hour": "5-hour window", "seven_day": "Weekly"}
            for key, w in windows.items():
                resets = f"  (resets {w['resets_at']})" if w.get("resets_at") else ""
                print(f"  {labels.get(key, key):<16} {w['utilization']:5.1f}% used{resets}")
        else:
            print("  Quota gauge unavailable (run 'claude login' or check network).")

        totals = await state.usage_totals()
        print()
        print(f"  {'Period':<10} {'Calls':>7} {'Input':>12} {'Output':>10} {'Cache read':>12} {'Est. cost':>10}")
        for label, key in (("Today", "today"), ("7 days", "week"), ("30 days", "month")):
            t = totals.get(key) or {}
            print(
                f"  {label:<10} {t.get('calls', 0):>7} "
                f"{t.get('input_tokens', 0):>12,} {t.get('output_tokens', 0):>10,} "
                f"{t.get('cache_read_tokens', 0):>12,} ${t.get('cost_usd', 0.0):>9.2f}"
            )

        by_agent = await state.usage_by_agent(days=args.days)
        if by_agent:
            print(f"\n  By agent (last {args.days} days):")
            for row in by_agent:
                print(
                    f"    {row['agent']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens  ${row['cost_usd']:.2f}"
                )

        by_task = await state.usage_by_task(days=args.days)
        if by_task:
            print(f"\n  By task (last {args.days} days):")
            for row in by_task[:10]:
                print(
                    f"    {row['task']:<22} {row['calls']:>5} calls  ${row['cost_usd']:.2f}"
                )
        print(
            "\n  Costs are equivalent API list prices — what this usage would"
            "\n  have cost without your subscription.\n"
        )

    asyncio.run(_usage())


def cmd_export(args):
    """Export the prospect list as a sequencer-ready CSV."""
    from harvey.state import StateManager
    from harvey.export import export_prospects_csv

    async def _export():
        state = StateManager()
        await state.init_db()

        email_statuses = None
        if args.email_status:
            email_statuses = [s.strip() for s in args.email_status.split(",") if s.strip()]
        statuses = None
        if args.status:
            statuses = [s.strip() for s in args.status.split(",") if s.strip()]

        count, _ = await export_prospects_csv(
            state,
            out_path=args.out,
            email_statuses=email_statuses,
            min_score=args.min_score,
            statuses=statuses,
            include_all=args.all,
        )
        scope = "all prospects" if args.all else (
            f"email status {email_statuses or ['verified', 'risky']}"
            + (f", score >= {args.min_score}" if args.min_score else "")
        )
        print(f"\n  Exported {count} prospect(s) to {args.out}  ({scope})")
        if count == 0 and not args.all:
            print("  Tip: no deliverable emails yet? Add a REOON_API_KEY to .env so")
            print("  Harvey can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into Instantly, Smartlead, or any sequencer.\n")

    asyncio.run(_export())


def main():
    from harvey.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="harvey",
        description="Harvey — Autonomous AI Sales Agent. Always Be Closing.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # harvey install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # harvey setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # harvey run
    sub = subparsers.add_parser("run", help="Start Harvey's heartbeat loop")
    sub.set_defaults(func=cmd_run)

    # harvey train <url>
    sub = subparsers.add_parser("train", help="Train Harvey on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.set_defaults(func=cmd_train)

    # harvey dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.set_defaults(func=cmd_dashboard)

    # harvey status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.set_defaults(func=cmd_status)

    # harvey export
    sub = subparsers.add_parser(
        "export", help="Export prospects as a sequencer-ready CSV"
    )
    sub.add_argument("--out", default="prospects.csv", help="Output file (default: prospects.csv)")
    sub.add_argument(
        "--email-status", default="",
        help="Comma-separated statuses to include (default: verified,risky)",
    )
    sub.add_argument("--min-score", type=int, default=0, help="Minimum ICP score")
    sub.add_argument("--status", default="", help="Comma-separated pipeline statuses (e.g. new,queued)")
    sub.add_argument("--all", action="store_true", help="Export everything, no filters")
    sub.set_defaults(func=cmd_export)

    # harvey usage
    sub = subparsers.add_parser("usage", help="Show Claude usage and quota")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.add_argument(
        "--reconcile", action="store_true",
        help="Backfill usage from Claude Code transcripts first",
    )
    sub.set_defaults(func=cmd_usage)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    harvey install   — Install dependencies")
        print("    harvey setup     — Configure Harvey (first time)")
        print("    harvey run       — Start closing deals")
        print("    harvey dashboard — Open the web dashboard")
        print()
        sys.exit(0)

    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\n  Interrupted. Goodbye.")
        sys.exit(130)
    except Exception as e:
        # ConfigError and friends carry actionable messages — show them
        # cleanly instead of a raw traceback.
        from harvey.config import ConfigError

        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'harvey {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
