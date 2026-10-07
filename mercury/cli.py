"""Mercury Agent CLI — simple commands to install, setup, run, and manage Mercury."""

import argparse
import asyncio
import json
import subprocess
import sys
from pathlib import Path


MIN_PYTHON = (3, 11)


def _check_python_version():
    """Refuse to install on a Python that cannot run Mercury.

    `python3 -m venv .venv` on macOS builds the venv from /usr/bin/python3,
    which is 3.9 — old enough that Mercury's `X | Y` type syntax fails at
    import. The install itself appears to succeed and the failure surfaces
    later as an unrelated-looking ImportError, so check up front and name the
    fix.
    """
    if sys.version_info >= MIN_PYTHON:
        return

    have = f"{sys.version_info.major}.{sys.version_info.minor}"
    want = f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
    print(f"\n  Mercury needs Python {want} or newer. This is Python {have}.")
    print(f"  ({sys.executable})\n")
    print("  On macOS, `python3` is usually the system 3.9, so a venv built")
    print("  with it is 3.9 too. Build the venv from a newer Python instead:\n")
    print("    brew install python@3.13")
    print("    rm -rf .venv")
    print("    $(brew --prefix)/bin/python3.13 -m venv .venv")
    print("    source .venv/bin/activate && pip install -e .\n")
    sys.exit(1)


def cmd_install(args):
    """Install all dependencies including Playwright browsers."""
    _check_python_version()
    print("\n  Installing Mercury dependencies...\n")

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
    print("  Mercury is installed. Run 'mercury setup' next.\n")


def _ensure_importable(project_root: str):
    """Make sure `import mercury` works outside the repo directory.

    Python 3.13 silently skips .pth files carrying the macOS 'hidden'
    file flag, and some Macs propagate that flag to everything inside
    dot-directories like .venv — which breaks editable installs. When
    that happens, fall back to symlinking the package into site-packages
    (imports don't check the hidden flag; only .pth parsing does).
    """
    check = subprocess.run(
        [sys.executable, "-c", "import mercury"],
        cwd="/", capture_output=True,
    )
    if check.returncode == 0:
        return

    try:
        import site
        site_packages = Path(site.getsitepackages()[0])
        link = site_packages / "mercury"
        target = Path(project_root) / "mercury"
        if not link.exists() and target.is_dir():
            link.symlink_to(target)
            recheck = subprocess.run(
                [sys.executable, "-c", "import mercury"],
                cwd="/", capture_output=True,
            )
            if recheck.returncode == 0:
                print("  ✓ Fixed package visibility (editable .pth was being "
                      "ignored; linked the package directly).\n")
                return
    except OSError as e:
        print(f"  Could not apply import fix: {e}")

    print("\n  Warning: 'import mercury' fails outside the project directory.")
    print("  Run mercury commands from the project root, or reinstall with:")
    print("    pip install -e . --config-settings editable_mode=compat\n")


def cmd_setup(args):
    """Run the interactive setup wizard."""
    from mercury.setup import run_setup

    asyncio.run(run_setup())


def cmd_run(args):
    """Start Mercury's heartbeat loop, or run a single cycle."""
    if getattr(args, "once", False):
        from mercury.main import run_once_main

        raise SystemExit(
            run_once_main(ignore_quiet_hours=getattr(args, "ignore_quiet_hours", False))
        )

    from mercury.main import main

    main()


def cmd_train(args):
    """Train Mercury on a website."""
    from mercury.trainer import Trainer

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
    from mercury.dashboard import start_dashboard

    if not 1 <= args.port <= 65535:
        print(f"  Invalid port: {args.port}. Must be 1-65535.")
        sys.exit(2)

    start_dashboard(host=args.host, port=args.port)


def cmd_status(args):
    """Show current pipeline status."""
    from mercury.state import StateManager

    async def _status():
        state = StateManager()
        await state.init_db()
        summary = await state.get_state_summary()

        print("\n  Mercury Pipeline Status")
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
    from mercury.state import StateManager

    async def _usage():
        state = StateManager()
        await state.init_db()

        # Live quota (best-effort; undocumented endpoint)
        from mercury.integrations.quota import QuotaClient
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
        print(f"  {'Period':<10} {'Calls':>7} {'Input':>12} {'Output':>10} {'Cache read':>12}")
        for label, key in (("Today", "today"), ("7 days", "week"), ("30 days", "month")):
            t = totals.get(key) or {}
            print(
                f"  {label:<10} {t.get('calls', 0):>7} "
                f"{t.get('input_tokens', 0):>12,} {t.get('output_tokens', 0):>10,} "
                f"{t.get('cache_read_tokens', 0):>12,}"
            )

        by_agent = await state.usage_by_agent(days=args.days)
        if by_agent:
            print(f"\n  By agent (last {args.days} days):")
            for row in by_agent:
                print(
                    f"    {row['agent']:<14} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )

        by_task = await state.usage_by_task(days=args.days)
        if by_task:
            print(f"\n  By task (last {args.days} days):")
            for row in by_task[:10]:
                print(
                    f"    {row['task']:<22} {row['calls']:>5} calls  "
                    f"{row['output_tokens']:>10,} out tokens"
                )
        print(
            "\n  Subscription plans aren't billed per token — these are usage"
            "\n  counts, not costs.\n"
        )

    asyncio.run(_usage())


def cmd_export(args):
    """Export the prospect list as a sequencer-ready CSV."""
    from mercury.state import StateManager
    from mercury.export import export_prospects_csv

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
            print("  Mercury can verify addresses, or use --all for the raw list.\n")
        else:
            print("  The CSV imports directly into Instantly, Smartlead, or any sequencer.\n")

    asyncio.run(_export())


def cmd_gmail(args):
    """Gmail provider utilities (auth / test)."""
    from mercury.config import load_env

    env = load_env()
    if args.gmail_action == "auth":
        from mercury.integrations.gmail import run_auth_flow
        ok = run_auth_flow(env.gmail_client_id, env.gmail_client_secret)
        sys.exit(0 if ok else 1)

    if args.gmail_action == "test":
        from mercury.config import load_config
        from mercury.integrations.gmail import GmailProvider

        async def _test():
            provider = GmailProvider(load_config(), env)
            ok, detail = await provider.test_connection()
            print(f"\n  {'✓' if ok else '✗'} {detail}\n")
            sys.exit(0 if ok else 1)
        asyncio.run(_test())


def cmd_mail(args):
    """Test whichever mail provider is configured.

    `mercury gmail test` only ever builds a GmailProvider, so an SMTP
    deployment had no way to check its credentials before trusting the
    pipeline with them -- even though test_connection() is on the base
    class and both providers implement it.
    """
    from mercury.config import load_config, load_env
    from mercury.integrations.mailboxes import MailboxPool, local_today

    if args.mail_action == "placement":
        cmd_mail_placement(args)
        return

    config = load_config()
    env = load_env()
    pool = MailboxPool.from_config(config, env)
    if pool is None:
        name = config.channels.email.provider or "(unset)"
        print(f"\n  ✗ No native mail provider for '{name}'.")
        print("    Set channels.email.provider to 'gmail' or 'smtp'.\n")
        sys.exit(1)

    async def _test():
        today = local_today(config)
        all_ok = True
        print()
        for mb in pool.mailboxes:
            ok, detail = await mb.provider.test_connection()
            all_ok = all_ok and ok
            cap = pool.cap_on(mb, today)
            label = f"{mb.email}  (cap today: {cap})  " if len(pool.mailboxes) > 1 else ""
            print(f"  {'✓' if ok else '✗'} {label}{detail}")
        print()
        sys.exit(0 if all_ok else 1)

    asyncio.run(_test())


def _placement_json(rep):
    """A placement report without non-serializable bits, for --json."""
    return json.loads(json.dumps(rep, default=str)) if rep else None


def cmd_mail_placement(args):
    """Inbox placement test: send email 1 to seed inboxes, read where it landed."""
    from mercury import placement
    from mercury.config import load_config, load_env
    from mercury.integrations.mailboxes import MailboxPool
    from mercury.state import StateManager

    config = load_config()
    env = load_env()
    action = args.placement_action or "run"

    async def _run():
        state = StateManager()
        await state.init_db()
        await placement.ensure_schema(state.db_path)

        if action in ("show", "check", "mark"):
            run_id = await placement.resolve_run(state, args.run_id)
            if not run_id:
                print("\n  No placement test found"
                      + (f" matching '{args.run_id}'." if args.run_id else
                         ". Run one with: mercury mail placement") + "\n")
                sys.exit(1)
            if action == "check":
                readers = placement.default_readers(placement.seeds_from_config(config, env))
                await placement.check(state, run_id, readers)
                await placement.finish(state, run_id)
            elif action == "mark":
                if not (args.seed and args.sender and args.folder):
                    print("\n  mark needs --seed, --sender (an address, or 'control') and --folder.\n")
                    sys.exit(2)
                try:
                    n = await placement.mark(state, run_id, args.seed, args.sender, args.folder)
                except placement.PlacementError as e:
                    print(f"\n  {e}\n")
                    sys.exit(2)
                if not n:
                    print(f"\n  No copy from {args.sender} to {args.seed} in run {run_id}.\n")
                    sys.exit(1)
            rep = await placement.report(state, run_id)
            if args.json:
                print(json.dumps(_placement_json(rep), indent=2))
            else:
                print(placement.format_report(rep))
            return

        pool = MailboxPool.from_config(config, env)
        if args.subject or args.body_file:
            if not (args.subject and args.body_file):
                print("\n  --subject and --body-file go together.\n")
                sys.exit(2)
            with open(args.body_file, encoding="utf-8") as f:
                email = {"id": "", "subject": args.subject, "body": f.read()}
        else:
            email = await placement.pick_email(state, args.outbox_id)
        if not email:
            print("\n  No email 1 to test" + (f" matching '{args.outbox_id}'" if args.outbox_id else "")
                  + ". Draft a campaign first, or pass --subject and --body-file.\n")
            sys.exit(1)

        p = placement.plan(config, env, pool, args.mailbox)
        print("\n  Placement test")
        print("  " + "=" * 52)
        print(f"  Email:   {email['subject']!r}"
              + (f"  (outbox {email['id'][:8]})" if email.get("id") else ""))
        for f in p["fleet"]:
            print(f"  From:    {f['email']}" + ("" if f["configured"] else "  (no password, skipped)"))
        if p["control"]:
            c = p["control"]
            print(f"  Control: {c['email']}" + ("" if c["configured"] else "  (not ready, skipped)"))
        else:
            print("  Control: none configured (a spam result will be read as the copy)")
        for s in p["seeds"]:
            print(f"  Seed:    {s.email}  ({s.provider}"
                  + (", read over IMAP)" if s.readable else ", record by hand)"))
        senders = sum(1 for f in p["fleet"] if f["configured"]) + (
            1 if p["control"] and p["control"]["configured"] else 0)
        print(f"  {senders * len(p['seeds'])} test emails. None of them touch the outbox or a daily cap.")
        if p["problems"]:
            print()
            for problem in p["problems"]:
                print(f"  ✗ {problem}")
            print()
            sys.exit(1)
        if args.dry_run:
            print("\n  Dry run: nothing sent.\n")
            return
        print()
        try:
            res = await placement.run(
                state, config, env, pool, subject=email["subject"], body=email["body"],
                only=args.mailbox, wait_seconds=args.wait,
                progress=lambda msg: print(f"  {msg}"),
            )
        except placement.PlacementError as e:
            print(f"\n  ✗ {e}\n")
            sys.exit(1)
        rep = await placement.report(state, res["run_id"])
        if args.json:
            print(json.dumps(_placement_json(rep), indent=2))
        else:
            print(placement.format_report(rep))

    asyncio.run(_run())


def cmd_health(args):
    """Deliverability health: a verdict per sending domain."""
    from mercury import deliverability, placement
    from mercury.config import load_config, load_env
    from mercury.integrations.mailboxes import MailboxPool
    from mercury.state import StateManager

    config = load_config()
    try:
        pool = MailboxPool.from_config(config, load_env())
    except Exception:
        pool = None

    async def _run():
        state = StateManager()
        await state.init_db()
        report = await deliverability.domain_report(state, config, pool)
        last = await placement.report(state)
        if args.json:
            print(json.dumps({**report, "placement": _placement_json(last)}, indent=2))
        else:
            print(deliverability.format_report(report, last))

    asyncio.run(_run())


def cmd_outbox(args):
    """Review and approve queued outgoing emails."""
    from mercury.state import StateManager

    async def _outbox():
        state = StateManager()
        await state.init_db()

        if args.approve_all:
            n = await state.approve_outbox()
            print(f"\n  Approved {n} email(s). They'll send on schedule.\n")
            return
        if args.approve:
            n = await state.approve_outbox(args.approve)
            print(f"\n  {'Approved.' if n else 'No pending item with that id.'}\n")
            return
        if args.reject:
            n = await state.reject_outbox_item(args.reject)
            print(f"\n  Rejected {n} email(s), later steps of the same sequence included.\n"
                  if n else "\n  No queued item with that id.\n")
            return

        paused = await state.get_setting("sending_paused")
        if paused:
            print(f"\n  ⚠ SENDING PAUSED: {paused}")
            print("  Resume with: mercury sending resume")

        pending = await state.get_outbox(status="pending_review", limit=50)
        approved = await state.get_outbox(status="approved", limit=10)
        print(f"\n  Outbox — {len(pending)} awaiting approval, "
              f"{len(approved)}+ approved/scheduled")
        print("  " + "=" * 60)
        for item in pending:
            print(f"\n  [{item['id']}] step {item['step']} ({item['kind']}) "
                  f"→ {item['to_email']}  (send {item['send_at'][:16]})")
            print(f"  Subject: {item['subject']}")
            body_preview = (item["body"][:200] + "...") if len(item["body"]) > 200 else item["body"]
            for line in body_preview.splitlines():
                print(f"    {line}")
        if pending:
            print("\n  Approve: mercury outbox --approve <id>   |   all: mercury outbox --approve-all")
            print("  Reject:  mercury outbox --reject <id>\n")
        else:
            print("  Nothing awaiting review.\n")

    asyncio.run(_outbox())


def cmd_profile(args):
    """Read what companies' own websites say about them. Free, no Claude calls."""
    from mercury.state import StateManager
    from mercury.pipeline import run_profile_stage

    async def _profile():
        state = StateManager()
        await state.init_db()

        pending = await state.count_companies_needing_profile(args.stale_days)
        if not pending:
            print("\n  Nothing to profile. Every company with a website has "
                  "been read recently.\n")
            return

        print(f"\n  {pending} companies need profiling. Reading up to "
              f"{args.limit}...\n")
        companies, observations, _ = await run_profile_stage(
            state, limit=args.limit, stale_days=args.stale_days)
        print(f"  Read {companies} sites → {observations} observations (free)")
        print("\n  See what turned up: mercury dashboard → Signals\n")

    asyncio.run(_profile())


def cmd_discover(args):
    """Find businesses. The only stage that spends money, so it estimates first."""
    from mercury.state import StateManager
    from mercury.config import load_config
    from mercury.collectors.discover import (
        PROVIDERS, build_queries, provider_menu, run_discovery,
    )

    async def _discover():
        if args.providers:
            print("\n  Discovery providers")
            print("  " + "=" * 68)
            for m in provider_menu():
                mark = "ready" if m["configured"] else "needs setup"
                print(f"\n  {m['label']}  [{m['key']}]  ({mark})")
                print(f"    {m['blurb']}")
                print(f"    Cost: {m['cost_note']}")
                print(f"    Free: {m['free_tier']}")
                if m["env_keys"]:
                    print(f"    Needs: {', '.join(m['env_keys'])} in .env  —  {m['signup_url']}")
                if m["caveat"]:
                    print(f"    Note: {m['caveat']}")
            print("\n  Run one with: mercury discover --provider <key> --estimate\n")
            return

        if args.provider not in PROVIDERS:
            print(f"\n  Unknown provider {args.provider!r}. "
                  f"See: mercury discover --providers\n")
            return

        config = load_config()
        state = StateManager()
        await state.init_db()

        # Semicolons, not commas: "Denver, CO" is one city, not two.
        cities = [c.strip() for c in args.city.split(";") if c.strip()] or None
        queries = build_queries(config, cities=cities,
                                depth=args.depth, limit=args.limit)

        report = await run_discovery(state, config, args.provider, queries,
                                     max_spend=args.max_spend, dry_run=True)
        print(f"\n  {len(queries)} queries via {PROVIDERS[args.provider].label}")
        for q in queries[:8]:
            print(f"    - {q.keyword()}" + ("" if q.coordinate else
                  "   (no coordinates — add icp.geo_coordinates for radius search)"))
        if len(queries) > 8:
            print(f"    ... and {len(queries) - 8} more")
        print(f"\n  Estimated cost: ${report.estimated_cost:.4f}"
              f"   (cap: ${args.max_spend:.2f})")

        if args.estimate:
            print("\n  Estimate only. Re-run without --estimate to collect.\n")
            return

        print("\n  Running...\n")
        from mercury.pipeline import run_prospecting

        result = await run_prospecting(
            state, config, args.provider, queries,
            max_spend=args.max_spend, profile=not args.no_profile,
        )
        r = result.discover or {}
        print(f"  DISCOVER — {r.get('found', 0)} results across "
              f"{r.get('queries', 0)} queries")
        print(f"    {r.get('new_companies', 0)} new companies, "
              f"{r.get('known_companies', 0)} already known")
        print(f"    {r.get('junk', 0)} filtered out as directories/aggregators")
        print(f"    {r.get('observations', 0)} observations recorded")
        print(f"    actual cost: ${r.get('actual_cost', 0):.4f}")
        if r.get("stopped"):
            print(f"    stopped early: {r['stopped']}")

        if args.no_profile:
            print("\n  PROFILE — skipped (--no-profile)")
        else:
            print(f"\n  PROFILE — {result.profiled_companies} sites read, "
                  f"{result.profile_observations} observations  (free)")

        for err in result.errors[:5]:
            print(f"    ! {err}")
        print("\n  Next: mercury dashboard → Signals → cohort builder\n")

    asyncio.run(_discover())


def cmd_signals(args):
    """Review the signal vocabulary — Mercury proposes, you confirm.

    Nothing is collected until a signal is confirmed, so a fresh install
    prospects against nothing until someone makes these decisions.
    """
    from mercury.state import StateManager
    from mercury.signals import seed_signal_catalog

    async def _signals():
        state = StateManager()
        await state.init_db()
        await seed_signal_catalog(state)

        codes = [a.strip().upper() for a in (args.confirm or args.reject or "").split(",")
                 if a.strip()]
        if codes:
            status = "confirmed" if args.confirm else "rejected"
            if codes == ["ALL"]:
                codes = [c["code"] for c in await state.get_signal_codes()]
            elif codes == ["FREE"]:
                # Only signals whose cost note STARTS with free/included. A
                # substring match would sweep in "1 credit (free tiers
                # available)", which is not free.
                codes = [c["code"] for c in await state.get_signal_codes()
                         if (c["cost_note"] or "").lower().startswith(("free", "included"))]
            changed = sum(
                [1 for c in codes if await state.set_signal_status(c, status)]
            )
            print(f"\n  {changed} signal(s) {status}.\n")
            return

        rows = await state.get_signal_codes()
        counts = {c["signal_code"]: c for c in await state.signal_counts()}
        by_status = {"confirmed": 0, "proposed": 0, "rejected": 0}
        for r in rows:
            by_status[r["status"]] = by_status.get(r["status"], 0) + 1

        print(f"\n  Signals — {by_status['confirmed']} confirmed, "
              f"{by_status['proposed']} awaiting you, {by_status['rejected']} off")
        print("  " + "=" * 66)
        mark = {"confirmed": "[on] ", "proposed": "[ ? ]", "rejected": "[off]"}
        current = None
        for r in rows:
            if r["category"] != current:
                current = r["category"]
                print(f"\n  {current.upper()}")
            seen = counts.get(r["code"], {}).get("companies", 0)
            seen_txt = f"  ({seen} companies)" if seen else ""
            print(f"    {mark.get(r['status'], '     ')} {r['code']:<22} "
                  f"{r['label']}{seen_txt}")
            print(f"          {r['cost_note']}")

        if by_status["proposed"]:
            print("\n  Confirm with: mercury signals --confirm CODE[,CODE...]")
            print("  Everything free: mercury signals --confirm free")
            print("  Details and descriptions: mercury dashboard → Signals\n")
        else:
            print("")

    asyncio.run(_signals())


def _persona_text(value, path):
    """A long field from a flag, a file, or stdin when the file is '-'."""
    from mercury.personas import PersonaError
    if not path:
        return value
    if path == "-":
        return sys.stdin.read()
    try:
        return Path(path).read_text()
    except OSError as error:
        raise PersonaError("invalid", f"Cannot read {path}: {error.strerror or error}") from error


def cmd_personas(args):
    """Writing personas: list, edit, version and preview them."""
    import json

    from mercury.config import load_config, load_env
    from mercury.control.personas import PersonaService, avatar_seed
    from mercury.personas import PersonaError
    from mercury.state import StateManager

    def fields():
        if args.instructions_file == "-" and args.examples_file == "-":
            raise PersonaError("invalid", "stdin can only be used for one of --instructions-file/--examples-file")
        changes = {"name": args.name, "description": args.description, "tone": args.tone,
                   "instructions": _persona_text(args.instructions, args.instructions_file),
                   "examples": _persona_text(args.examples, args.examples_file),
                   "avatar_seed": avatar_seed(args.avatar) if args.avatar else None,
                   "sign_name": args.sign_name}
        return {key: value for key, value in changes.items() if value is not None}

    def line(p):
        flags = [label for label, on in (("default", p["is_default"]), ("archived", p["archived"])) if on]
        print(f"  {p['name']:<28} v{p['revision']:<4} {p['id'][:8]}  {', '.join(flags)}")

    def detail(p):
        print(f"\n  {p['name']}  v{p['revision']}" + ("  (default)" if p["is_default"] else "")
              + ("  (archived)" if p["archived"] else ""))
        print(f"  id {p['id']} · avatar {p['avatar_seed']}")
        if p.get("description"):
            print(f"  {p['description']}")
        for label, key in (("Tone", "tone"), ("Writing preferences", "instructions"), ("Style examples", "examples")):
            print(f"\n  {label}")
            for text in (p.get(key) or "(none)").splitlines():
                print(f"    {text}")
        print()

    async def _run():
        service = await PersonaService(StateManager(), load_config(), load_env()).ready()
        action = args.persona_action or "list"
        if action == "list":
            result = await service.list(include_archived=args.all)
            if args.json:
                return print(json.dumps(result, indent=2))
            print(f"\n  Personas ({len(result['personas'])})")
            print("  " + "=" * 60)
            for p in result["personas"]:
                line(p)
            print("\n  Details: mercury personas show NAME\n")
        elif action == "show":
            p = await service.get(args.persona)
            if args.json:
                return print(json.dumps(p, indent=2))
            detail(p)
        elif action == "versions":
            versions = await service.versions(args.persona)
            if args.json:
                return print(json.dumps(versions, indent=2))
            print()
            for v in versions:
                print(f"  v{v['revision']:<4} {v['created_at'][:16]}  {v['tone'][:60]}")
            print()
        elif action in ("create", "edit"):
            if action == "create":
                p = await service.create(fields())
                if args.default:
                    p = await service.set_default(p["id"])
            else:
                changes = fields()
                if not changes:
                    raise PersonaError("invalid", "Nothing to change. Pass --tone, --instructions, --name ...")
                before = await service.find(args.persona)
                p = await service.update(args.persona, changes, args.expected_revision)
                if p["revision"] == before["revision"]:
                    print(f"\n  Updated {p['name']}. Writing unchanged, still v{p['revision']}.\n")
                    return
            if args.json:
                return print(json.dumps(p, indent=2))
            print(f"\n  Saved {p['name']} as v{p['revision']}. New drafts use it from the next generation.\n")
        elif action == "mailboxes":
            mailboxes = await service.mailboxes()
            if args.json:
                return print(json.dumps(mailboxes, indent=2, default=str))
            print(f"\n  Sending mailboxes ({len(mailboxes)})")
            print("  " + "=" * 60)
            for m in mailboxes:
                voice = m["persona"]["name"] + (" (default)" if m["follows_default"] else "")
                print(f"  {m['email']:<34} {voice:<26} signs as {m['signer']}"
                      + ("" if m["enabled"] else "  [no new threads]"))
            print("\n  Change one: mercury personas assign EMAIL [PERSONA] [--sign-name NAME]\n")
        elif action == "assign":
            current = next((m for m in await service.mailboxes() if m["email"] == args.email.strip().lower()), None)
            persona = "" if args.default else args.persona or (current or {}).get("persona_id", "")
            sign_name = (current or {}).get("sign_name", "") if args.sign_name is None else args.sign_name
            m = await service.assign_mailbox(args.email, persona, sign_name)
            voice = m["persona"]["name"] + (" (default)" if m["follows_default"] else "")
            print(f"\n  {m['email']} now writes as {voice} and signs as {m['signer']}.\n")
        elif action == "default":
            p = await service.set_default(args.persona)
            print(f"\n  New drafts now use {p['name']} (v{p['revision']}).\n")
        elif action in ("archive", "restore"):
            p = await service.set_archived(args.persona, action == "archive")
            print(f"\n  {p['name']} {'archived' if p['archived'] else 'restored'}.\n")
        elif action in ("prompt", "preview"):
            command = service.prompt if action == "prompt" else service.preview
            if action == "preview":
                print("\n  Writing a sample (one Claude call, nothing is queued)...")
            result = await command(args.persona, args.contact, args.version, args.instruction)
            if args.json:
                return print(json.dumps(result, indent=2, default=str))
            persona = result["persona"]
            print(f"\n  {persona['name']} v{persona['revision']}")
            print("  " + "=" * 60)
            if action == "prompt":
                print(result["prompt"])
            else:
                print(f"  Subject: {result['subject']}\n")
                for text in result["body"].splitlines():
                    print(f"    {text}")
            print()

    try:
        asyncio.run(_run())
    except PersonaError as error:
        print(f"\n  {error}\n", file=sys.stderr)
        sys.exit(1)


def _import_mapping(pairs):
    """--map email="Work Email" --map company_name=Company -> {field: header}."""
    if not pairs:
        return None
    mapping = {}
    for pair in pairs:
        name, sep, header = pair.partition("=")
        if not sep:
            raise SystemExit(f"\n  --map takes FIELD=HEADER, got {pair!r}\n")
        mapping[name.strip()] = header.strip()
    return mapping


def _print_import_rows(rows, show_all):
    shown = [r for r in rows if show_all or r["outcome"] != "new" or r["action"] != "create"]
    if not shown:
        return
    print(f"\n  {'Row':>5}  {'Outcome':<11} {'Action':<9} Detail")
    for r in shown[:200]:
        detail = r.get("email") or ""
        if r.get("reason"):
            detail = f"{detail}  {r['reason']}".strip()
        if r.get("fill"):
            detail += f"  (fills {', '.join(r['fill'])})"
        print(f"  {r['row']:>5}  {r['outcome']:<11} {r['action']:<9} {detail}")
    if len(shown) > 200:
        print(f"  ... {len(shown) - 200} more (use --json for every row)")


def cmd_import(args):
    """Import contacts from a CSV: preview, then commit unless --dry-run."""
    import json

    from mercury.control.imports import ImportService
    from mercury.csv_import import MAX_BYTES, ImportFileError
    from mercury.state import StateManager

    def read_file():
        if args.file == "-":
            return sys.stdin.buffer.read()
        path = Path(args.file)
        try:
            size = path.stat().st_size
        except OSError as error:
            raise SystemExit(f"\n  Cannot read {args.file}: {error.strerror or error}\n")
        if size > MAX_BYTES:
            raise SystemExit(f"\n  {args.file} is {size / 1048576:.1f} MB, over the "
                             f"{MAX_BYTES // 1048576} MB limit; split it and import the parts.\n")
        try:
            return path.read_bytes()
        except OSError as error:
            raise SystemExit(f"\n  Cannot read {args.file}: {error.strerror or error}\n")

    async def _run():
        data = read_file()
        service = await ImportService(StateManager()).ready()
        try:
            exclude_rows = [int(x) for x in args.exclude_rows.split(",") if x.strip()]
        except ValueError:
            raise SystemExit(f"\n  --exclude-rows takes row numbers, got {args.exclude_rows!r}\n")
        options = dict(filename=Path(args.file).name, mapping=_import_mapping(args.map),
                       delimiter=args.delimiter, policy=args.policy, exclude_rows=exclude_rows)
        if args.dry_run:
            result = await service.preview(data, **options)
            if args.json:
                return print(json.dumps(result, indent=2))
            c = result["counts"]
            print(f"\n  {args.file}: {c['total']} rows, {result['delimiter']}-separated")
            print("  Columns: " + ", ".join(f"{k}={v!r}" for k, v in result["mapping"].items()))
            if result["ignored_columns"]:
                print(f"  Ignored {', '.join(result['ignored_columns'])}: imported addresses "
                      "are unverified until Mercury verifies them.")
            print(f"\n  New {c['new']} · needs enrichment {c['incomplete']} · duplicate "
                  f"{c['duplicate']} · invalid {c['invalid']}")
            print(f"  Would create {c['create']}, fill {c['fill']}, skip {c['skip']}, "
                  f"leave out {c['exclude']}")
            _print_import_rows(result["rows"], args.all_rows)
            print("\n  Dry run: nothing was changed. Drop --dry-run to import.\n")
            return
        result = await service.commit(data, skip_invalid=args.skip_invalid, origin="cli", **options)
        if args.json:
            return print(json.dumps(result, indent=2))
        b = result["batch"]
        if result["already_committed"]:
            print(f"\n  Already imported as batch {b['id']} on {b['created_at'][:16]}. Nothing changed.\n")
            return
        print(f"\n  Imported batch {b['id']}: created {b['created']}, filled {b['filled']}, "
              f"skipped {b['skipped']}, left out {b['excluded']}")
        _print_import_rows(result["rows"], args.all_rows)
        print("\n  Imported contacts are held: nothing is drafted or sent for them.")
        print(f"  Next: mercury imports verify {b['id']}   then   mercury imports release {b['id']}\n")

    try:
        asyncio.run(_run())
    except ImportFileError as error:
        print(f"\n  {error}")
        if error.details.get("headers"):
            print("  Columns in this file: " + ", ".join(error.details["headers"]))
        if error.details.get("rows"):
            print("  Invalid rows: " + ", ".join(map(str, error.details["rows"])))
            print("  Re-run with --dry-run to see why, or --skip-invalid to import the rest.")
        if error.code == "ambiguous_delimiter":
            print("  Pass --delimiter " + " or --delimiter ".join(error.details["candidates"]))
        print()
        sys.exit(1)


def cmd_imports(args):
    """List import batches, verify their addresses, release them to outreach."""
    import json

    from mercury.config import load_env
    from mercury.control.imports import ImportService
    from mercury.csv_import import ImportFileError
    from mercury.state import StateManager

    async def _run():
        service = await ImportService(StateManager(), load_env()).ready()
        action = args.imports_action or "list"
        if action != "list" and not args.batch:
            raise ImportFileError("not_found", f"Which batch? mercury imports {action} BATCH_ID")
        if action == "list":
            batches = await service.batches(args.limit)
            if args.json:
                return print(json.dumps(batches, indent=2))
            if not batches:
                return print("\n  No imports yet. Try: mercury import contacts.csv --dry-run\n")
            print(f"\n  {'Batch':<13} {'When':<17} {'Created':>7} {'Held':>5} {'Unverified':>10}  File")
            for b in batches:
                print(f"  {b['id']:<13} {b['created_at'][:16]:<17} {b['created']:>7} {b['held']:>5} "
                      f"{b['unverified']:>10}  {b['filename']}")
            print()
        elif action == "show":
            result = await service.batch(args.batch)
            if args.json:
                return print(json.dumps(result, indent=2))
            b = result["batch"]
            print(f"\n  Batch {b['id']}  {b['filename']}  ({b['origin']}, {b['policy']} duplicates)")
            for c in result["contacts"]:
                print(f"    {c['n']:>5}  {c['status']:<10} email {c['email_status'] or 'unknown'}")
            _print_import_rows(result["rows"], args.all_rows)
            print()
        elif action == "verify":
            estimate = await service.verify_estimate(args.batch)
            if args.estimate:
                return print(json.dumps(estimate, indent=2) if args.json else f"\n  {estimate['cost']}\n")
            if not estimate["providers"]:
                raise ImportFileError("no_verifier", estimate["cost"])
            if not estimate["addresses"]:
                return print("\n  Every address in this batch is already verified or settled.\n")
            print(f"\n  {estimate['cost']}")
            totals, after, results, unresolved = 0, 0, {}, 0

            async def progress(done, total):
                print(f"\r  Verifying {totals + done}/{estimate['addresses']}", end="", flush=True)

            while True:
                step = await service.verify(args.batch, limit=args.limit, after_row=after,
                                            progress=None if args.json else progress)
                totals += step["checked"]
                unresolved += step.get("unresolved", 0)
                after = step["next_after_row"]
                for k, v in step["results"].items():
                    results[k] = results.get(k, 0) + v
                if step["stopped"] or not step["remaining"] or not step["checked"] or not args.all:
                    break
            summary = {"checked": totals, "results": results, "remaining": step["remaining"],
                       "unresolved": unresolved, "stopped": step["stopped"]}
            if args.json:
                return print(json.dumps(summary, indent=2))
            print("\n  " + (", ".join(f"{v} {k}" for k, v in results.items()) or "nothing checked"))
            if step["stopped"]:
                print(f"  {step['stopped']}")
            elif step["remaining"]:
                print(f"  {step['remaining']} left. Run again, or pass --all.")
            if unresolved:
                print(f"  {unresolved} could not be settled and stay unverified. Verifying again "
                      "spends credits on them again.")
            print(f"  Release the verified ones: mercury imports release {estimate['batch_id']}\n")
        elif action == "release":
            result = await service.release(args.batch, include_risky=args.include_risky)
            if args.json:
                return print(json.dumps(result, indent=2))
            print(f"\n  Released {result['released']} contact(s) to outreach; "
                  f"{result['still_held']} still held (unverified or invalid).")
            if result["released"]:
                print("  The Writer drafts for them on its next cycle; drafts wait in the Outbox.")
            print()

    try:
        asyncio.run(_run())
    except ImportFileError as error:
        print(f"\n  {error}\n")
        sys.exit(1)


def cmd_sending(args):
    """Pause/resume the sending kill switch."""
    from mercury.state import StateManager

    async def _run():
        state = StateManager()
        await state.init_db()
        if args.sending_action == "pause":
            await state.set_setting("sending_paused", "paused manually")
            print("\n  Sending paused. Nothing will leave the outbox.\n")
        elif args.sending_action == "resume":
            await state.set_setting("sending_paused", "")
            await state.set_setting("bounce_count", "0")
            print("\n  Sending resumed (bounce counter reset).\n")
        else:
            paused = await state.get_setting("sending_paused")
            print(f"\n  Sending: {'PAUSED — ' + paused if paused else 'active'}\n")

    asyncio.run(_run())


def main():
    from mercury.paths import PROJECT_ROOT
    project_root = str(PROJECT_ROOT)

    parser = argparse.ArgumentParser(
        prog="mercury",
        description="Mercury Agent: an autonomous outreach agent that runs on your Claude subscription.",
    )
    subparsers = parser.add_subparsers(dest="command")

    # mercury install
    sub = subparsers.add_parser("install", help="Install dependencies")
    sub.set_defaults(func=cmd_install)

    # mercury setup
    sub = subparsers.add_parser("setup", help="Run the interactive setup wizard")
    sub.set_defaults(func=cmd_setup)

    # mercury run
    sub = subparsers.add_parser("run", help="Start Mercury's heartbeat loop")
    sub.add_argument(
        "--once",
        action="store_true",
        help="Run a single cycle and exit (for cron/scheduled runs)",
    )
    sub.add_argument(
        "--ignore-quiet-hours",
        action="store_true",
        help="With --once: run even during quiet hours",
    )
    sub.set_defaults(func=cmd_run)

    # mercury train <url>
    sub = subparsers.add_parser("train", help="Train Mercury on a website")
    sub.add_argument("url", help="Website URL to crawl and learn from")
    sub.add_argument(
        "max_pages",
        nargs="?",
        type=int,
        default=100,
        help="Max pages to crawl (default: 100)",
    )
    sub.set_defaults(func=cmd_train)

    # mercury dashboard
    sub = subparsers.add_parser("dashboard", help="Open the web dashboard")
    sub.add_argument("--host", default="127.0.0.1", help="Host (default: 127.0.0.1)")
    sub.add_argument("--port", type=int, default=5555, help="Port (default: 5555)")
    sub.set_defaults(func=cmd_dashboard)

    # mercury status
    sub = subparsers.add_parser("status", help="Show pipeline status")
    sub.set_defaults(func=cmd_status)

    # mercury export
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

    # mercury gmail auth|test
    sub = subparsers.add_parser("gmail", help="Gmail provider setup")
    sub.add_argument("gmail_action", choices=["auth", "test"],
                     help="auth: one-time OAuth; test: verify connection")
    sub.set_defaults(func=cmd_gmail)

    # mercury mail test | mercury mail placement [run|show|check|mark] [RUN]
    sub = subparsers.add_parser(
        "mail", help="Test the mail provider, or run an inbox placement test")
    sub.add_argument(
        "mail_action", choices=["test", "placement"],
        help="test: verify send/receive credentials; placement: send email 1 to your "
             "seed inboxes and read where it landed",
    )
    sub.add_argument("placement_action", nargs="?", default="run",
                     choices=["run", "show", "check", "mark"],
                     help="placement: run a test (default), show a result, check the "
                          "seeds again, or mark a folder by hand")
    sub.add_argument("run_id", nargs="?", default="", help="Placement run id (default: the last)")
    sub.add_argument("--dry-run", action="store_true", help="Show who would send to whom; send nothing")
    sub.add_argument("--mailbox", action="append", metavar="EMAIL_OR_DOMAIN",
                     help="Only test these mailboxes or domains (repeatable)")
    sub.add_argument("--outbox-id", default="", help="Test this outbox email instead of the newest email 1")
    sub.add_argument("--subject", default="", help="Test this subject (with --body-file)")
    sub.add_argument("--body-file", default="", help="Test the body in this file (with --subject)")
    sub.add_argument("--wait", type=int, default=None,
                     help="Seconds to look for the emails in the seeds (default: placement.wait_seconds)")
    sub.add_argument("--seed", default="", help="mark: the seed inbox")
    sub.add_argument("--sender", default="", help="mark: the address it came from, or 'control'")
    sub.add_argument("--folder", default="", help="mark: primary, inbox, promotions, other_tab, spam, missing")
    sub.add_argument("--json", action="store_true", help="Machine-readable output")
    sub.set_defaults(func=cmd_mail)

    # mercury health
    sub = subparsers.add_parser(
        "health", help="Deliverability verdict per sending domain (the 1%% rule)")
    sub.add_argument("--json", action="store_true", help="Machine-readable output")
    sub.set_defaults(func=cmd_health)

    # mercury outbox
    sub = subparsers.add_parser("outbox", help="Review/approve queued emails")
    sub.add_argument("--approve", metavar="ID", default="", help="Approve one item")
    sub.add_argument("--approve-all", action="store_true", help="Approve all pending")
    sub.add_argument("--reject", metavar="ID", default="", help="Reject one item")
    sub.set_defaults(func=cmd_outbox)

    # mercury import FILE / mercury imports
    sub = subparsers.add_parser(
        "import", help="Import contacts from a CSV (preview with --dry-run)")
    sub.add_argument("file", help="CSV file (UTF-8), or - for stdin")
    sub.add_argument("--dry-run", action="store_true", help="Preview only; change nothing")
    sub.add_argument("--map", action="append", metavar="FIELD=HEADER",
                     help="Map a field to a column, e.g. --map email='Work Email'. Repeatable; "
                          "replaces the automatic mapping. Fields: email, first_name, last_name, "
                          "full_name, title, company_name, website, industry, linkedin_url, "
                          "phone, personalization")
    sub.add_argument("--delimiter", default="", choices=["", "comma", "semicolon", "tab"],
                     help="Column delimiter when detection is ambiguous")
    sub.add_argument("--policy", default="skip", choices=["skip", "fill"],
                     help="Existing contacts: skip them (default) or fill their blank fields")
    sub.add_argument("--skip-invalid", action="store_true",
                     help="Leave invalid rows out and import the rest")
    sub.add_argument("--exclude-rows", default="", metavar="N,N",
                     help="Row numbers to leave out (row 1 is the header)")
    sub.add_argument("--all-rows", action="store_true", help="List every row, not just problems")
    sub.add_argument("--json", action="store_true", help="Machine-readable result")
    sub.set_defaults(func=cmd_import)

    sub = subparsers.add_parser("imports", help="Import batches: list, show, verify, release")
    sub.add_argument("imports_action", nargs="?", default="list",
                     choices=["list", "show", "verify", "release"])
    sub.add_argument("batch", nargs="?", default="", help="Batch id (or a unique prefix)")
    sub.add_argument("--estimate", action="store_true", help="verify: show the cost only")
    sub.add_argument("--all", action="store_true", help="verify: keep going until done")
    sub.add_argument("--limit", type=int, default=25, help="Batches to list / addresses per verify run")
    sub.add_argument("--include-risky", action="store_true",
                     help="release: also release catch-all (risky) addresses")
    sub.add_argument("--all-rows", action="store_true", help="show: list every row")
    sub.add_argument("--json", action="store_true", help="Machine-readable output")
    sub.set_defaults(func=cmd_imports)

    # mercury sending pause|resume|status
    sub = subparsers.add_parser("discover", help="Find businesses matching your ICP")
    sub.add_argument("--provider", default="osm",
                     help="Discovery provider (default: osm — free, no account)")
    sub.add_argument("--providers", action="store_true",
                     help="List every provider with cost and setup, then exit")
    sub.add_argument("--estimate", action="store_true",
                     help="Print projected spend and exit without calling anything")
    sub.add_argument("--city", default="",
                     help='Semicolon-separated cities, e.g. "Denver, CO;Dallas, TX" '
                          '(default: icp.geography)')
    sub.add_argument("--depth", type=int, default=30,
                     help="SERP depth (default: 30 — depth 100 costs 10x since Sept 2025)")
    sub.add_argument("--limit", type=int, default=100,
                     help="Max records per query (default: 100)")
    sub.add_argument("--max-spend", type=float, default=1.0,
                     help="Hard cap in dollars (default: 1.00)")
    sub.add_argument("--no-profile", action="store_true",
                     help="Don't profile what was found (profiling is free)")
    sub.set_defaults(func=cmd_discover)

    sub = subparsers.add_parser(
        "profile", help="Read discovered companies' websites (free, no Claude)")
    sub.add_argument("--limit", type=int, default=200,
                     help="Max sites to read (default: 200)")
    sub.add_argument("--stale-days", type=int, default=90,
                     help="Re-read a site after this many days (default: 90)")
    sub.set_defaults(func=cmd_profile)

    sub = subparsers.add_parser(
        "signals", help="Review/confirm which signals Mercury prospects against")
    sub.add_argument("--confirm", metavar="CODES", default="",
                     help="Comma-separated codes to confirm ('all' or 'free' accepted)")
    sub.add_argument("--reject", metavar="CODES", default="",
                     help="Comma-separated codes to turn off")
    sub.set_defaults(func=cmd_signals)

    sub = subparsers.add_parser("personas", help="List, edit and preview writing personas")
    personas = sub.add_subparsers(dest="persona_action")
    sub.set_defaults(func=cmd_personas, persona_action=None, all=False, json=False)

    def persona_parser(name, help, persona="required"):
        p = personas.add_parser(name, help=help)
        if persona == "required":
            p.add_argument("persona", help="Name, id or id prefix")
        elif persona == "optional":
            p.add_argument("persona", nargs="?", default="", help="Name or id (default persona if omitted)")
        p.add_argument("--json", action="store_true", help="Machine-readable output")
        return p

    def writing_flags(p, create):
        p.add_argument("--name", required=create, default=None)
        p.add_argument("--tone", required=create, default=None, help="One line, repeated in every prompt")
        p.add_argument("--description", default=None)
        p.add_argument("--instructions", default=None, help="Writing preferences")
        p.add_argument("--instructions-file", default="", help="Read preferences from a file ('-' for stdin)")
        p.add_argument("--examples", default=None, help="Style examples")
        p.add_argument("--examples-file", default="", help="Read examples from a file ('-' for stdin)")
        p.add_argument("--avatar", default="", help="1-24 or a seed name (random if omitted)")
        p.add_argument("--sign-name", default=None, help="Suggested sign-off for mailboxes that set none")

    p = persona_parser("list", "All personas", persona=None)
    p.add_argument("--all", action="store_true", help="Include archived personas")
    persona_parser("show", "One persona's writing settings")
    persona_parser("versions", "Version history")
    p = persona_parser("create", "Create a persona", persona=None)
    writing_flags(p, create=True)
    p.add_argument("--default", action="store_true", help="Use it for new drafts")
    p = persona_parser("edit", "Change a persona. Tone, preferences or examples create a new version")
    writing_flags(p, create=False)
    p.add_argument("--expected-revision", type=int, default=None,
                   help="Fail if someone saved a newer version first")
    persona_parser("mailboxes", "Each sending mailbox's voice and sign-off name", persona=None)
    p = persona_parser("assign", "Give a mailbox a voice and a sign-off", persona=None)
    p.add_argument("email", help="Sending mailbox address")
    p.add_argument("persona", nargs="?", default="", help="Name or id (omit to keep or follow the default)")
    p.add_argument("--sign-name", default=None,
                   help="Name its emails are signed with ('' uses the persona's suggestion)")
    p.add_argument("--default", action="store_true", help="Follow the default persona instead")
    persona_parser("default", "Use this persona for new drafts")
    persona_parser("archive", "Hide a persona from new drafts")
    persona_parser("restore", "Bring back an archived persona")
    for action, help in (("prompt", "Show the exact writer prompt (no model call)"),
                         ("preview", "Write one sample email (one Claude call, never queued)")):
        p = persona_parser(action, help, persona="optional")
        p.add_argument("--contact", required=True, help="Contact id or email")
        p.add_argument("--version", type=int, default=None, help="Revision number (default: latest)")
        p.add_argument("--instruction", default="", help='One-off instruction, e.g. "shorter"')

    sub = subparsers.add_parser("sending", help="Kill switch: pause/resume sending")
    sub.add_argument("sending_action", nargs="?", default="status",
                     choices=["pause", "resume", "status"])
    sub.set_defaults(func=cmd_sending)

    # mercury usage
    sub = subparsers.add_parser("usage", help="Show Claude usage and quota")
    sub.add_argument("--days", type=int, default=30, help="Breakdown window (default: 30)")
    sub.set_defaults(func=cmd_usage)

    args = parser.parse_args()
    args._project_root = project_root

    if args.command is None:
        parser.print_help()
        print("\n  Quick start:")
        print("    mercury install   — Install dependencies")
        print("    mercury setup     — Configure Mercury (first time)")
        print("    mercury run       — Start closing deals")
        print("    mercury dashboard — Open the web dashboard")
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
        from mercury.config import ConfigError

        if isinstance(e, ConfigError):
            print(f"\n  Configuration problem:\n  {e}\n")
        else:
            print(f"\n  Error running 'mercury {args.command}': {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
