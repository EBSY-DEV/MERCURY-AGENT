# Architecture

Mercury Agent is a single Python process that wakes up on a timer, decides what to do from the state of a SQLite database, runs a few small agents, and goes back to sleep. Model work goes through the `claude` CLI in headless mode; everything that can be done without a model (search, scraping, verification, sending, scheduling, gating) is plain Python. This page describes the components, how data flows between them, how the Claude budget is enforced, and how to extend Mercury with a new discovery provider, signal or dashboard view.

## Components

| Component | Where | What it does |
|---|---|---|
| CLI | `mercury/cli.py` | Every `mercury <command>`. Thin wrappers over the modules below. |
| Heartbeat | `mercury/main.py` | The loop: quiet hours, budget, decide, act, log, sleep. `run_once()` is the single-cycle entry point; both share `run_cycle()`. |
| Brain | `mercury/brain.py` | Runs `claude -p <prompt> --output-format json --dangerously-skip-permissions` as a subprocess with a 300-second timeout and two retries. Records each call's tokens in `usage_events`. Loads prompts and skills. |
| Quota | `mercury/integrations/quota.py` | Reads the subscription's 5-hour and weekly utilization from the same endpoint Claude Code's `/usage` uses, with the locally stored OAuth token. Undocumented, so every failure degrades to "unknown". |
| Agents | `mercury/agents/` | Scout, Writer, Sender, Handler, Analyst (below). |
| Collectors | `mercury/collectors/discover.py`, `profile.py`; `mercury/pipeline.py` | DISCOVER (paid, provider adapters) and PROFILE (free HTML reading), chained by database queries rather than hand-offs. |
| Signals | `mercury/signals.py` | The proposed signal catalog. |
| State | `mercury/state.py` | `StateManager`: every read and write, the schema, and migrations. |
| Mail | `mercury/integrations/mail_provider.py`, `gmail.py`, `smtp_mail.py`, `instantly.py` | Provider adapters. `mailboxes.py` is the mailbox pool: rotation, caps, warm-up ramp. |
| Gate | `mercury/gate.py` | Deterministic pre-send checks. |
| Warm-up | `mercury/warmup.py` | Health gates, the week-by-week plan, DNS checks. |
| Metrics | `mercury/metrics.py` | Sent / replies / positive / bounces definitions shared by the trend chart, heatmap and health gates. |
| Email finding | `mercury/integrations/email_finder.py` | Pattern-first address discovery and single-candidate verification. |
| Dashboard | `mercury/dashboard.py`, `mercury/web/` | FastAPI JSON API plus plain HTML, CSS and JS served from disk. |
| Trainer | `mercury/trainer.py` | `mercury train`: crawl a site, write `mercury.local.yaml` and product skills. |
| Paths | `mercury/paths.py` | Project root resolution and the one-time Harvey-to-Mercury file renames. |

### Agents

- **Scout** finds people. Python does the searching (Serper, Tavily, DuckDuckGo, Bing, Google), the scraping, the inbox sweep over discovered companies, tech detection and email resolution; Claude only scores and personalizes contacts that were already found.
- **Writer** turns verified `new` prospects into three-step sequences (opener, follow-up, break-up) and stores them as draft campaigns. It can also regenerate a single outbox email on request.
- **Sender** stages draft campaigns into the outbox and drains due, approved rows through the mailbox pool and the gate. On the legacy Instantly path it deploys campaigns through the Instantly API instead.
- **Handler** polls inboxes, separates bounces from human replies, classifies intent, advances conversation stages, queues replies, handles bounces and the kill switch.
- **Analyst** writes `data/analytics.json` with pipeline and campaign stats. No Claude calls.

## Data flow

```
                    you: confirm signals, approve email, move deals
                                   │
                                   ▼
┌────────────┐   companies    ┌──────────────┐  observations  ┌──────────────┐
│  DISCOVER  │──────────────> │   PROFILE    │──────────────> │   signals /  │
│ (providers,│  observations  │ (homepage,   │                │ observations │
│  paid)     │                │  sitemap...) │                └──────────────┘
└────────────┘                └──────────────┘                       │
                                                                      ▼
                          ┌──────────────────────────────────────────────┐
                          │ SCOUT: inbox sweep, search, team pages,      │
                          │ email pattern + verify ─> prospects          │
                          │ (verified / risky / guess / invalid)         │
                          └──────────────────────────────────────────────┘
                                              │ verified prospects (status new)
                                              ▼
                          ┌──────────────────────────────────────────────┐
                          │ WRITER (Claude) ─> draft campaign, 3 steps    │
                          └──────────────────────────────────────────────┘
                                              │
                                              ▼
                          ┌──────────────────────────────────────────────┐
                          │ SENDER: stage ─> outbox (pending_review)      │
                          │   approve ─> approved ─> drain:               │
                          │   kill switch, stop-on-reply, step order,     │
                          │   mailbox pick + caps + health gates,         │
                          │   pre-send gate, footer ─> provider ─> sent   │
                          └──────────────────────────────────────────────┘
                                              │
                                              ▼
                          ┌──────────────────────────────────────────────┐
                          │ HANDLER: poll inboxes ─> bounce? mark invalid,│
                          │   cancel queue, kill switch                   │
                          │   reply? classify (Claude), cancel queue,     │
                          │   conversation stage, queue reply ─> outbox   │
                          └──────────────────────────────────────────────┘
```

Every arrow is a read or write against `data/mercury.db`. Nothing is passed in memory between stages, so any stage can be re-run, a crashed run resumes on the next cycle, and the dashboard sees exactly what the agents see.

## The heartbeat

Each cycle (`run_cycle` in `mercury/main.py`):

1. **Quiet hours.** The loop sleeps until they end. `run --once` exits with code 0 instead.
2. **Budget.** `Brain.is_within_budget` compares the worse of the 5-hour and weekly quota windows against `usage.max_daily_claude_percent`. If the quota can't be read, it falls back to Mercury's own calls today against `200 x percent / 100`. Over budget, model work is skipped but the zero-Claude tasks still run, and the loop sleeps an hour before re-checking.
3. **Decide.** `decide_next_action` uses deterministic rules over pipeline counts, not a model call:

   | Priority | Action | When |
   |---|---|---|
   | 1 | `send_campaign` | Approved, unsent outbox emails to sendable addresses exist, sending isn't paused, and today's capacity isn't used up. |
   | 2 | `write_campaign` | Writable prospects exist (status `new`, a verified address, or risky with `send_to_risky`), and the queue holds less than seven days of first emails. |
   | 3 | `prospect` | Fewer than 20 writable prospects. |
   | 4 | `idle` | Otherwise: run the Analyst. |

4. **Act, with ride-alongs.** Alongside the primary action, every cycle also:
   - drains the outbox (native providers),
   - polls inboxes and handles replies and bounces (native providers, when within budget),
   - profiles up to 25 unprofiled companies (free),
   - runs the Scout's inbox sweep over discovered companies with no contact yet,
   - starts the daily background discovery if one is due (it runs as a separate task and never blocks a cycle).

   Tasks run concurrently with `asyncio.gather`; one failing agent is logged and never takes down the cycle.
5. **Log** the action to `actions` and sleep `heartbeat_interval_minutes`. Consecutive failures back off 60 s, 120 s, 240 s, ... up to 15 minutes.

Replies are not a priority level any more: inbox polling rides along every cycle, so an open conversation can't starve writing and prospecting.

## Usage tracking

The Brain parses each `claude -p` call's JSON result and writes one `usage_events` row: agent, task, model, input, output and cache tokens. That ledger contains only Mercury's own calls; Mercury never scans your other Claude Code sessions. The Usage tab and `mercury usage` read it, together with the live quota windows. There are no dollar figures, since subscription plans are not billed per token.

When the CLI runs as root (typical in hosted runners), the Brain sets `IS_SANDBOX=1` so `--dangerously-skip-permissions` is accepted. The Docker image runs as an unprivileged user and doesn't need it.

## State and migrations

`StateManager` opens SQLite in WAL mode (so the dashboard reads while the agent writes) with a 30-second busy timeout. Main tables:

| Table | Holds |
|---|---|
| `companies` | Businesses, keyed by normalised domain or provider id. |
| `prospects` | People, with `email_status` and pipeline `status` (`new`, `queued`, `contacted`, `replied`, `meeting`, `closed`, `lost`, `opted_out`). |
| `campaigns` | Draft and active sequences. |
| `outbox` | Every native email: kind (`sequence` or `reply`), step, schedule, status, sending mailbox, provider ids. |
| `conversations` | Reply threads, intent, stage. |
| `signal_codes`, `observations` | The signal vocabulary and every fact collected. A trigger rejects observations with unknown codes. |
| `runs` | Collector runs: provider, records, cost, status. |
| `actions` | The event log (also the source for reply and bounce metrics). |
| `usage_events` | Per-call token usage. |
| `email_patterns` | Learned address patterns per domain. |
| `settings` | Key/value flags: `operator_pause` (your pause), `sending_paused` (the bounce kill switch, a health hold), `bounce_count`, `discovery_provider`, cached geocodes and DNS results. See `mercury/holds.py`. |
| `warmup_inboxes` | The warm-up overlay: manual or automatic pause, checklist, notes. |

Migrations are a list of SQL scripts in `MIGRATIONS` (`mercury/state.py`). The schema version is `PRAGMA user_version`; `init_db()` takes a write lock, re-reads the version, and applies each pending script and its version bump inside one transaction. Never edit or reorder a released migration; append a new one.

## The dashboard

`mercury/dashboard.py` is a FastAPI app with JSON endpoints under `/api/...`. The UI is `mercury/web/index.html`, `app.css` and `app.js`, served from disk on every request, plus vendored fonts. There is no build step: edit `app.css` or `app.js` and reload. Static files are served with `Cache-Control: no-store`, fonts are cached.

The dashboard computes mailbox caps, health gates and metrics with the same functions the sender uses (`MailboxPool`, `warmup.apply_health`, `metrics`), so the numbers you see are the numbers that are enforced.

## Extending Mercury

### A discovery provider

Subclass `DiscoveryProvider` in `mercury/collectors/discover.py` and register it in `PROVIDERS`:

```python
class Acme(DiscoveryProvider):
    key = "acme"                          # used by --provider and the dashboard
    label = "Acme Local Data"
    kind = "listings"                     # "listings" (radius search) or "serp" (rank)
    blurb = "One sentence on what it returns."
    cost_note = "$0.50 per 1,000 businesses"
    free_tier = "1,000 records/month"
    signup_url = "https://example.com"
    env_keys = ("acme_api_key",)          # EnvConfig field names
    caveat = "Anything a user should know before paying."

    def estimate(self, queries):
        return sum(0.0005 * q.limit for q in queries)

    async def fetch(self, client, env, query):
        r = await client.get("https://api.example.com/search",
                             params={"q": query.term, "near": query.coordinate},
                             headers={"Authorization": env["acme_api_key"]})
        r.raise_for_status()
        self.last_cost = float(r.json().get("cost", 0.0))   # actual spend for the run log
        return [Business(name=i["name"], domain=normalize_domain(i.get("website", "")),
                         website=i.get("website", ""), phone=i.get("phone", ""),
                         provider=self.key, external_id=f"acme:{i['id']}")
                for i in r.json()["results"]]
```

`env` is `load_env().model_dump()`, so a new key also needs a field in `EnvConfig` and a line in `load_env()` (`mercury/config.py`), and an entry in `.env.example`. The run loop handles the estimate check, spend cap, stop switch, junk filtering, entity resolution and observations. Add tests next to `tests/test_discover.py`, which uses a fake provider.

### A signal

1. Add an entry to `SIGNAL_CATALOG` in `mercury/signals.py`: `code`, `label`, `description`, `category` (`discovery`, `profile`, `people` or `verification`), `value_type` (`num`, `text` or `bool`), `collector`, `cost_note` (start it with "free" if it is, so `--confirm free` picks it up), and optionally `confidence_floor`.
2. Emit it from a collector, only when confirmed. In the profiler, `add("MY_SIGNAL", num=1)` inside `profile_company` already checks the confirmed set. Elsewhere, check `await state.confirmed_signal_codes()` and write rows with `state.add_observations()`.

No migration is needed: the catalog is seeded on load and the new signal appears as `proposed`. Codes not in `signal_codes` are rejected by the database trigger, so a collector must never write a code that is not in the catalog.

### A dashboard view

1. Add an endpoint in `mercury/dashboard.py` that returns JSON. Use `_state()` for a `StateManager` and call `await state.init_db()` first, or `query_db()` for read-only SQL.
2. In `mercury/web/index.html`, add a nav button (`data-tab="mytab"`) and a `<div id="mytab" class="section">`.
3. In `mercury/web/app.js`, add a `loadMyTab()` that calls `api('/api/mytab')` and renders, and a `case 'mytab'` in `loadCurrentTab()` (and in the 15-second refresh switch at the bottom of the file if it should stay live).

## Testing

```bash
pip install -e ".[dev]"
pytest -q
```

The suite runs offline in a few seconds. Patterns used throughout `tests/`:

- **A real database in a temp dir.** `StateManager(str(tmp_path / "x.db"))` and `init_db()`; no mocking of SQL.
- **Fakes at the boundary.** `FakeProvider` (a `MailProvider` that records sends and returns canned inbound messages) in `tests/test_outbox_native.py`, a fake `DiscoveryProvider` in `tests/test_discover.py`, a fake brain whose `think()` returns fixed text, and a fake DNS resolver in `tests/test_warmup.py`.
- **Config builders.** `make_config()` in `tests/test_mailboxes.py` builds a `MercuryConfig` with mailboxes in code.
- **Dashboard endpoints** through FastAPI's `TestClient`, with `monkeypatch.setattr(mercury.dashboard, "DB_PATH", tmp_db)`.
- **Async code** is driven with `asyncio.run(...)` in small helpers; `pytest-asyncio` is available for async tests.

Tests never touch the network, never call `claude`, and never send email.
