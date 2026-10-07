# Mercury Agent — Autonomous Outreach Agent

Mercury Agent is an autonomous outreach agent powered by Claude Code. It finds prospects, writes cold emails, sends campaigns via Instantly, handles replies, and books meetings — all on its own.

**You (Claude) are the guide.** When someone opens this project, your job is to help them understand what Mercury is, get it configured, and start closing deals. Be conversational, not robotic. Explain things simply. Ask one thing at a time.

---

## When Someone First Opens This Project

Start by checking what state things are in. Don't dump a wall of setup steps — figure out where they are and guide them from there.

### Quick health check (do this silently):
1. Does `.venv/` exist? → If not, they need install
2. Is `mercury` importable? → If not, dependencies need installing
3. Does `.env` exist with real values? → If not, they need API keys
4. Does `mercury.yaml` have real values (not "Your Company")? → If not, they need product training
5. Does `data/mercury.db` exist? → If not, Mercury hasn't run yet

### Then introduce yourself based on what you find:

**If nothing is set up:**
> "This is Mercury — an autonomous AI sales agent. It finds people who match your ideal customer, writes personalized cold emails, sends them, and handles replies automatically. It runs on your Claude Max subscription so there's no extra cost.
>
> Let me help you get it set up. It takes about 5 minutes. First, let me install the dependencies..."

**If partially set up:**
> "Looks like Mercury is partially configured. [specific thing] is done but [specific thing] still needs setting up. Want me to pick up where you left off?"

**If fully set up:**
> "Mercury is configured and ready to go. Want me to start it, show you the dashboard, or explain how it works?"

---

## Explaining Mercury to Users

People will ask "how does this work?" — explain it simply:

- **"What does Mercury do?"** → It's like having a tireless sales assistant. Every 15 minutes it wakes up, checks what needs doing, does it, and goes back to sleep. It finds prospects, writes emails, sends campaigns, and responds to replies.

- **"How does it find people?"** → It searches the web (DuckDuckGo, Bing, Google) for companies matching your target profile, visits their websites, and finds team members. For emails, it learns each company's address pattern (from their site, or a free Hunter domain lookup) and verifies a single candidate through a free verification tier — every address is tagged verified / catch-all / guess, and only deliverable ones get sent. No expensive tools needed.

- **"How does it write emails?"** → It uses proven cold email frameworks (like AIDA and PAS) with strict rules — short, personal, no AI-sounding language. Each email is tailored to the specific person and their company.

- **"Is it safe?"** → Yes. It runs locally on your machine, has daily usage limits, quiet hours, and send limits built in. It can't delete files, access your bank account, or do anything outside its sales workflow. Everything it does is logged in a local SQLite database you can inspect anytime.

- **"What does it cost?"** → Just your Claude Max subscription (which you already have). The only paid integration is Instantly for sending emails (their cheapest plan works). Everything else — prospecting, email writing, reply handling — is included.

- **"What's the dashboard?"** → Run `mercury dashboard` to see a local web UI at localhost:5555. It opens on **Today** — anything waiting on a decision, then the pipeline. Tabs for Signals, Companies, Contacts, Pipeline, Calendar, Campaigns, Outbox, Mailboxes, Conversations, Activity, Usage, Settings. Mailboxes lists every sending inbox grouped by domain, with its warm-up ramp, health, DNS and checklist; an inbox's drawer pauses/resumes it and edits its settings (saved to `channels.email.mailboxes`). For a populated demo: `python scripts/seed_demo.py`, then run the dashboard with the `MERCURY_DB_PATH` / `MERCURY_CONFIG` line it prints.

- **"What are signals?"** → Signals are the facts Mercury collects about a business: who its current agency is, whether it's running ads, whether it has online booking, how it ranks. **Mercury proposes; you confirm.** Nothing is collected until the user says yes on the Signals tab (or `mercury signals --confirm ...`). That keeps spend intentional and makes every prospect list explainable — a cohort is a query over signals a human chose, not a black box.

---

## Setup Flow

Walk through these steps conversationally. Ask one thing at a time. Don't overwhelm.

### Step 1: Install Dependencies

```bash
python3 -m venv .venv && source .venv/bin/activate && pip install -e .
```

Then install the browser for LinkedIn prospecting:
```bash
python -m playwright install chromium
```

### Step 2: API Keys (`.env`)

Mercury needs an **email provider** (to send + read replies) and, strongly recommended, an **email verifier**. Pick one provider:

```
# --- Email provider: choose ONE, set channels.email.provider to match ---
GMAIL_CLIENT_ID=            # Recommended: Gmail/Workspace. Then run 'mercury gmail auth'
GMAIL_CLIENT_SECRET=
# --- or SMTP+IMAP (AgentMail, Fastmail, Workspace app password) ---
SMTP_HOST=  SMTP_PORT=587  SMTP_USERNAME=  SMTP_PASSWORD=
IMAP_HOST=  IMAP_PORT=993
# --- or legacy Instantly ---
INSTANTLY_API_KEY=

# --- Email verification (add at least one; else emails stay 'guess') ---
REOON_API_KEY=              # 600 free/mo — best free tier
ZEROBOUNCE_API_KEY=         # 100 free/mo — best for M365/Workspace catch-alls
HUNTER_API_KEY=             # 50 free/mo + pattern lookup

# --- Discovery (optional — the free OpenStreetMap source needs no key) ---
DATAFORSEO_LOGIN=  DATAFORSEO_PASSWORD=   # cheapest local-business + SERP data
DATAFORSEO_SANDBOX=                       # any value routes to the free sandbox

# --- Optional ---
LINKEDIN_EMAIL=  LINKEDIN_PASSWORD=   # LinkedIn prospecting
CLOUDFLARE_ACCOUNT_ID=  CLOUDFLARE_API_TOKEN=   # deep JS crawling for training
SERPER_API_KEY=            # web search + discovery (2,500 free, then a $50 prepaid pack)
```

**Recommended provider — Gmail:** for <50 cold emails/day, a real Google Workspace mailbox on a *dedicated secondary domain* (never the main one) is the most deliverable, cheapest (~$7/mo) option. Set `channels.email.provider: gmail` in mercury.yaml, put the OAuth client id/secret in `.env`, then run `mercury gmail auth` (one-time browser login). SMTP works with any mailbox (AgentMail, Fastmail). Instantly still works as a legacy option.

**Email verification matters:** Mercury learns each company's email *pattern* and verifies ONE candidate rather than guessing (raw SMTP probing is not viable from a locally-running agent — outbound port 25 is usually blocked, and Google Workspace / Microsoft 365 accept everything from an unknown IP). Without a verifier key, found emails are marked `guess` and are **never sent**.

**Approval by default:** with a native provider, every outgoing email waits in the **Outbox** for your approval (dashboard Outbox tab, or `mercury outbox`). Once you trust the output, set `channels.email.require_approval: false` in mercury.yaml for full autopilot.

After getting the Instantly API key, test it:
```bash
source .venv/bin/activate && python3 -c "
import asyncio, httpx
async def test():
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.get('https://api.instantly.ai/api/v2/accounts', headers={'Authorization': 'Bearer API_KEY_HERE'})
        print('Connected!' if r.status_code == 200 else f'Failed: {r.status_code}')
asyncio.run(test())
"
```

### Step 3: Product Training

This is the most important step. Mercury needs to know what it's selling.

**Option A — Train from a website URL (recommended):**
```bash
source .venv/bin/activate && python -m mercury.trainer https://their-website.com
```
This generates: `mercury.yaml`, `skills/product_knowledge.md`, `skills/competitive_intel.md`

**Option B — Manual configuration:**
Ask the user these questions and build `mercury.yaml` and `skills/product_knowledge.md`:
- What's your company name?
- What do you sell? (product/service name and one-line description)
- What does it cost?
- What are the top 3-5 benefits?
- Who's your target? (industries, job titles, company size, geography)
- What name and email should Mercury use? (the "from" identity)
- What objections do you usually hear? How do you respond?
- **Offers & closing:**
  - What's the primary offer? (subscription, service, etc.)
  - Is there a low-commitment entry? (free trial, free audit, etc.)
  - What's the goal? (book a call, start a trial, get a reply)
  - How should meetings be booked? (calendar link, suggest times, ask preference)
  - How long is the call? (default: 15 minutes)
  - Who takes the meeting?

### Step 4: Confirm Signals

Mercury ships a catalog of ~19 signals it knows how to collect, all **proposed** and
none active. Nothing is prospected against until the user confirms them.

Send them to `mercury dashboard` → **Signals**, where each one shows a
plain-language description, what it costs, and how many companies already carry
it. Or from the terminal:

```bash
mercury signals                      # review the catalog
mercury signals --confirm free       # turn on everything that costs nothing
mercury signals --confirm SERP_RANK  # the one paid discovery signal
```

Most signals are free (they read pages a business already publishes). Only
`SERP_RANK` and `EMAIL_STATUS` consume credits — the cost note on each row says so.

Once signals are confirmed, the **cohort builder** at the bottom of the Signals
tab turns them into a target list: pick what a good prospect must have (and what
disqualifies them) and it counts the matches live.

### Step 5: Find Businesses

Discovery is the only stage that spends money, so it always estimates first.

```bash
mercury discover --providers                        # the menu, with real prices
mercury discover --estimate                         # projected spend, then exits
mercury discover                                    # free OpenStreetMap source
mercury discover --provider dataforseo_listings --max-spend 2.00
```

The dashboard's **Discover** tab is the same thing with the tradeoffs laid out
side by side: what each source does, what it costs, its free tier, and which
`.env` keys it needs. Nothing is called until the user presses Run.

| Source | Cost | Free tier | Best for |
|---|---|---|---|
| **OpenStreetMap** (default) | free | unlimited | Trying the whole pipeline with no account. Thin coverage for service-area trades — OSM maps premises, so it has under 2,000 roofers in the entire US. A trial source, not a complete list. |
| **DataForSEO Business Listings** | $0.372/1k businesses | $1 credit | Local trades, clinics, contractors. Phone + domain + rating + whether the listing is claimed, and it can filter for businesses with **no website at all**. |
| **DataForSEO SERP** | $0.0018 a search at depth 30 | $1 credit + free sandbox | Rank as the buying signal. Positions 11-30 are the sweet spot. |
| **Serper** | ~$0.30-$1.00/1k | 2,500 free, no card | The easiest paid one to try. |

Cities are geocoded automatically (cached forever, one lookup per city), or set
`icp.geo_coordinates` in mercury.yaml to control the radius:
`"Denver, CO": "39.7392,-104.9903,50"`.

**Depth 20-30, not 100.** Google removed 100-results-per-page in September 2025,
so depth 100 is now billed as ten pages at nearly every provider. Below rank 30
it is mostly directories anyway.

### Step 6: Behavior Settings

These go in `mercury.yaml` under `usage:`. Use sensible defaults unless they want to customize:
- `max_daily_claude_percent`: 80 (how much of daily Claude quota to use)
- `heartbeat_interval_minutes`: 15 (how often Mercury checks for work)
- `quiet_hours`: 22:00-07:00 in their timezone
- `max_daily_sends`: 50 (email send limit)

### Step 7: Start Mercury

```bash
source .venv/bin/activate && mercury run
```

Or open the dashboard:
```bash
mercury dashboard
```

### Step 8 (optional): Run it on a schedule instead

`mercury run` is a daemon — it wants a machine that stays up. To run Mercury on a
schedule in an ephemeral container (a cron job, a CI runner, a Claude Code
Routine) two things change:

- **`mercury run --once`** does a single cycle and exits, letting the scheduler
  own the cadence. Quiet hours still apply, and it never opens the setup wizard.
- **State needs somewhere to live.** `data/mercury.db` is the whole pipeline, and
  a fresh container has none. `scripts/cloud_run.sh` restores it from a *private*
  state repo, runs one cycle, and pushes it back.

Prospect data must never go in this public repository. See `docs/cloud.md` for
the full setup — state repo layout, environment variables, reviewing the outbox
remotely, and why a `SERPER_API_KEY` is close to mandatory from a datacenter IP.

---

## After Setup — Ongoing Help

Users will come back with questions and tasks. Common ones:

- **"Show me what Mercury has done"** → Run `mercury status` or `mercury dashboard`
- **"The emails aren't good"** → Edit `skills/email_frameworks.md` and `prompts/writer.md`. Show them the current rules and help them adjust.
- **"Mercury isn't finding the right people"** → Check the ICP config in `mercury.yaml`. Adjust industries, titles, company_size, geography.
- **"I want to change what Mercury says"** → Skills are in `skills/`, prompts are in `prompts/`. Both are plain markdown files. Edit them directly.
- **"Train Mercury on a different product"** → `mercury train <new-url>`
- **"How do I see the database?"** → It's at `data/mercury.db`. They can open it with any SQLite tool, or ask you to query it.
- **"I already have a list"** → Contacts → **Import CSV** in the dashboard, or `mercury import leads.csv --dry-run` first. Imported contacts are held (status `imported`, address `guess`) until they run `mercury imports verify BATCH` and `mercury imports release BATCH`. Details in docs/prospecting.md#importing-a-list.
- **"I just want the prospect list"** → `mercury export` writes a sequencer-ready CSV (verified/risky emails only; `--all` for everything). Also available as Export buttons on the dashboard's Contacts tab. Mercury is valuable purely as a list-builder even if the user never lets it send.

---

## Design System (mandatory for any UI work)

Every change to the dashboard (`mercury/web/`) or to a design in `design/*.pen` must use the existing design system. Don't invent new visual patterns. If something you need isn't covered, extend the system in `app.css` (and the brand board in `design/mercury-brand.pen`) first, then use it.

**Sources of truth:** the tokens and component classes in `mercury/web/app.css` (see the rules in its header comment), and the "Mercury by EBSY" brand board in `design/mercury-brand.pen`.

**Hard rules:**
- **Status is a Phosphor icon plus a word.** Use `.badge t-good|t-waiting|t-bad|t-active|t-note|t-idle` (`toneBadge()` in `app.js`). The colour lives only in the glyph; the label stays `--text-2` (only `t-bad` labels go red). Never use tinted pills, coloured dots, or coloured background chips for status.
- **One accent.** Violet (`--accent`) marks what's selected, primary or interactive. Green, amber, red and blue only mean status. Don't use status colours for decoration or emphasis.
- **Tokens only.** Colours, fonts and radii come from CSS variables (`--panel`, `--text-2`, `--border`, `--r-sm`, `--r-md`, ...). No raw hex values in new CSS or markup, except the status glyph colours already defined in `.badge`.
- **Shape scale:** cards and panels 12px, controls 8px, badges and nav pills fully round. Nothing else.
- **Numbers are monospaced and tabular** (`var(--mono)` / `.num`).
- **Buttons:** primary is `.btn-primary` (accent fill), secondary is `.btn-secondary` (panel with a strong border). Don't make dark or custom-coloured buttons.
- **Icons:** Phosphor only (`icon('name')` in `app.js`, `library: "phosphor"` in Pencil).
- **Reuse existing components** before writing new ones: `.panel` / `.panel-head` / `.panel-foot`, `.kpi`, `.table-card`, `.drawer`, `.segmented`, `.toolbar`, `emptyState()`, `confirmModal()`, the chart helpers (`niceTicks`, `yAxis`, `chartHover`).
- **Copy:** plain sentences. No em-dashes in UI strings, and no all-caps eyebrow labels.
- **Light and dark:** every change has to work in both themes. Check both before you finish.

**Before calling UI work done:** look at it in the browser in light and dark mode, and confirm there's no tinted pill, no raw hex value and no new radius or colour that isn't a token.

---

## How Mercury Works (Technical Reference)

### Architecture
- **Heartbeat loop** (`main.py`): Every cycle → check quiet hours → check budget → decide → act → log → sleep
- **Brain** (`brain.py`): Wraps `claude -p --output-format json --dangerously-skip-permissions` for headless Claude calls; records exact per-call token usage into `usage_events`
- **State** (`state.py`): SQLite at `data/mercury.db` with tables for companies, prospects, campaigns, conversations, actions, usage
- **Usage tracking** (`usage.py`, `integrations/quota.py`): per-call attribution (agent/task/model/tokens) for Mercury's OWN Claude calls only — it never scans your other Claude Code sessions — plus a live subscription-quota gauge read the same way Claude Code's `/usage` does. The budget check throttles Mercury against real quota utilization so it always leaves headroom for your own interactive Claude use (`usage.max_daily_claude_percent`). Dashboard has a Usage tab; CLI has `mercury usage`.
- **Skills** (`skills/`): Markdown knowledge files injected into agent prompts
- **Signals** (`signals.py`, `state.py`): every fact Mercury learns is an OBSERVATION — a row in `observations`, never a column. A new signal needs no migration, re-observing over time is a free time series, and confidence + provenance travel with the fact. The vocabulary in `signal_codes` is governed (a trigger rejects any code not in it) and gated: only `status = 'confirmed'` signals are collected, and only a human sets that. `state.cohort(require, exclude)` does the set intersection in SQL.
- **Dashboard** (`dashboard.py` + `mercury/web/`): FastAPI JSON API plus plain HTML/CSS/JS served from disk — no build step. Edit `mercury/web/app.css` or `app.js` and reload the page.
- **DISCOVER** (`collectors/discover.py`): the only stage that spends money, so cost discipline lives in the collector, not the database — every run estimates first, checks a spend cap between batches, reads a kill switch, and flushes observations per batch. Providers are adapters behind one interface (`DiscoveryProvider`); adding one is a small class. Junk filtering matches *patterns*, not a literal blocklist. Entity resolution is the normalised domain, falling back to the provider's `external_id` for businesses with no website.

### Sub-Agents
- **Scout**: Python does all web searching (DuckDuckGo → Bing → Google → Serper API) and email resolution (pattern-first: cache → scraped mailto → Hunter domain search → default, then verify one candidate via Reoon/ZeroBounce/Hunter/SMTP; catch-alls flagged `risky`). Claude only scores/personalizes found data. Scout also collects **buying signals**: tech stack detected on each company's site (HubSpot, Shopify, Intercom, ~35 tools — zero extra requests) and hiring signals from careers pages. With `pip install python-jobspy` (optional), a job-board strategy discovers companies actively hiring for roles in `icp.hiring_signals` (falls back to `icp.titles`) — the strongest in-market signal. Signals land in prospects' personalization notes and boost their score.
- **Writer**: Generates 3-email sequences (Email 1 < 75 words, Email 2 < 75, Email 3 < 40). Strict ban list on AI patterns.
- **Sender**: Native providers (gmail/smtp) render merge vars per prospect and STAGE each email into the `outbox` (pending_review → approved → sent) with a scheduled send time; each heartbeat drains due, approved items with human-like pacing, the daily cap, the deterministic pre-send gate, and stop-on-reply. Legacy Instantly path deploys campaigns via API. Enforces daily send limits either way.
- **Handler**: Polls the provider (or Instantly) for replies, dedups them, classifies intent, advances conversation stage, and queues auto-responses through the same outbox approval ladder. On native providers it also detects bounces → marks the address invalid, cancels the prospect's queued sends, and trips a global kill switch past a bounce-rate threshold. A vacation auto-reply (not an acknowledgement or receipt) pauses that contact's remaining cold steps in `sequence_pauses` until the return date read by `mercury/ooo.py` (no date, or an ambiguous/past one, means "return date needs review" and the pause never ends by itself); a human reply, opt-out, bounce or closure supersedes it. The Outbox tab lists paused contacts and lets you set a date or resume.
- **Analyst**: Runs on idle cycles. Generates `data/analytics.json` with pipeline stats and insights.

### Conversation Stages
`initial_outreach → engaged → qualifying → presenting → negotiating → closing → closed_won / closed_lost`

### Priority Order
handle_replies > send_campaigns > write_campaigns > prospect > idle (run analyst)

### Key Commands
```bash
source .venv/bin/activate    # Always activate venv first
mercury run                   # Start the heartbeat loop
mercury run --once            # One cycle, then exit (cron / scheduled cloud runs)
mercury dashboard             # Web UI at http://localhost:5555
mercury setup                 # Re-run setup wizard
mercury train <url>           # Train on a product website
mercury status                # Pipeline summary
mercury usage                 # Claude quota gauges + per-agent token usage (no dollar costs — subscription plans aren't billed per token)
mercury export                # Deliverable prospects → sequencer-ready CSV (prospects.csv)
mercury export --all          # Full raw list, no filters
mercury import FILE --dry-run # Preview a CSV import; drop --dry-run to import (held, unverified)
mercury imports               # Import batches; verify BATCH / release BATCH
mercury gmail auth            # One-time Gmail OAuth (when provider: gmail)
mercury gmail test            # Verify the Gmail connection
mercury mail test             # Verify whichever provider is configured (gmail or smtp)
mercury outbox                # Review queued emails; --approve <id> / --approve-all / --reject <id>
mercury signals               # The signal vocabulary; --confirm / --reject CODES (or 'free' / 'all')
mercury personas              # Writing voices: list / show / create / edit / default / archive / versions / prompt / preview
mercury personas mailboxes    # Each sending mailbox's voice and sign-off; change one with: personas assign EMAIL [PERSONA] --sign-name NAME
mercury discover              # Find businesses; --providers / --estimate / --provider <key>
mercury mail limits [--strict] # Inbox limit warnings (2 per domain, provider ceilings, 14-day warm-up); `mercury run --strict` refuses to start on any
mercury sending pause|resume  # Kill switch for all outbound
mercury demos                 # Demo gate: who waits for a demo; ready / request / retire EMAIL_OR_ID
```

### Common Issues
- **"command not found: mercury"**: Activate venv first: `source .venv/bin/activate`
- **"ModuleNotFoundError: No module named 'mercury'" after install (macOS)**: Python 3.13 silently ignores `.pth` files that carry the macOS hidden file flag, and some Macs propagate that flag into `.venv`. Run `mercury install` again (it auto-fixes by linking the package), or manually: `ln -s "$(pwd)/mercury" .venv/lib/python3.13/site-packages/mercury`
- **"externally-managed-environment"**: Use a venv, not system Python
- **SQLite errors**: The `data/` directory is created automatically on first run
- **Claude headless mode fails**: User needs `claude login` and an active Max subscription
- **Instantly API 401**: Wrong API key or needs the Growth plan for API access
- **Google search rate limiting**: Mercury automatically falls back to DuckDuckGo and Bing. For reliable search, add a Serper API key ($5/mo).
