# Getting started

This guide takes you from a fresh clone to a running Mercury Agent: install, connect a mailbox, train Mercury on your product, confirm which signals matter, run a first discovery, approve the first emails, and start the heartbeat loop. Plan on about half an hour, most of it spent on mailbox setup and reading Mercury's first drafts.

## Prerequisites

| You need | Why |
|---|---|
| Python 3.11 or newer | Mercury uses 3.11+ syntax. `mercury install` refuses older interpreters. On macOS the system `python3` is often 3.9, so build the venv from a Homebrew Python. |
| Claude Code CLI, logged in to a Pro or Max plan | Every model call is `claude -p ...` in headless mode. Run `claude login` once. Mercury reads your live quota and stops at a configurable share of it. |
| A mailbox to send from, on a secondary domain | Gmail/Google Workspace (recommended) or any SMTP+IMAP mailbox. Use a domain that is not your main one, so a reputation problem never touches your real email. |
| An email verification key (strongly recommended) | Reoon, ZeroBounce or Hunter. Without one, most found addresses stay `guess` and are never sent. See [Verification](email-and-deliverability.md#address-verification). |

Optional: a DataForSEO or Serper account for paid discovery, LinkedIn credentials, Cloudflare Browser Rendering for deeper training crawls. None are needed to try the pipeline.

## 1. Install

```bash
git clone https://github.com/EBSY-DEV/MERCURY-AGENT.git mercury-agent && cd mercury-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
python -m playwright install chromium   # only needed for LinkedIn prospecting
```

`pip install -e .` installs the `mercury` command into the venv. Activate the venv in every new shell (`source .venv/bin/activate`), or call `.venv/bin/mercury` directly.

For the test suite, install the dev extras: `pip install -e ".[dev]"`.

## 2. Add credentials

```bash
cp .env.example .env
```

Fill in one email provider and at least one verifier. The full list of variables is in [Configuration](configuration.md#environment-variables-env).

**Gmail / Google Workspace (recommended):**

1. In Google Cloud Console, create a project and enable the Gmail API.
2. Configure the OAuth consent screen (internal or testing) and create an OAuth client of type "Desktop app".
3. Put the client id and secret in `.env` as `GMAIL_CLIENT_ID` and `GMAIL_CLIENT_SECRET`.
4. Run the one-time browser login. The refresh token is stored in `data/gmail_token.json`.

```bash
mercury gmail auth
mercury gmail test
```

**SMTP + IMAP (Fastmail, AgentMail, a Workspace app password, self-hosted):** set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD` and `IMAP_HOST`. IMAP username and password default to the SMTP ones. Then:

```bash
mercury mail test
```

`mercury mail test` works for either native provider and, with several mailboxes configured, tests each one and prints its cap for today.

## 3. Train Mercury on your product

```bash
mercury train https://your-company.com
mercury train https://your-company.com 200   # optional page limit (default 100)
```

The trainer crawls your site, then uses Claude to write:

- `mercury.local.yaml`: persona, product, ICP and default channel settings
- `skills/product_knowledge.md`: features, pricing, pain points, buying triggers
- `skills/competitive_intel.md`: battle cards, when competitors were identified

All three are gitignored. `mercury.local.yaml` takes precedence over the tracked `mercury.yaml` template, so your real configuration never gets committed by accident. See [Which config file is used](configuration.md#which-config-file-is-used).

**Review `mercury.local.yaml` before going further.** The trainer guesses some values and leaves others at defaults that you will want to change:

```yaml
persona:
  name: "Jordan Lee"                 # the trainer uses "Mercury"
  email: "jordan@yourcompany-mail.com"   # guessed from your domain; use the sending mailbox

channels:
  email:
    provider: "gmail"                # the trainer writes "instantly"; set gmail or smtp
    require_approval: true

compliance:
  postal_address: "123 Main St, Suite 4, Denver, CO 80202"   # required before anything sends
```

The native sender holds the whole outbox while `compliance.postal_address` is empty. See [Compliance](email-and-deliverability.md#compliance-footer-and-opt-outs).

If you prefer to answer questions instead of crawling, `mercury setup` runs an interactive wizard. It is oriented toward the legacy Instantly provider and writes `mercury.yaml`, so you will still need to set the provider and postal address by hand afterwards.

## 4. Confirm signals

Signals are the facts Mercury collects about a business: whether it runs ads, who built its site, whether it takes online bookings, how it ranks. Mercury ships a catalog of them, all marked *proposed*. Nothing is collected until you confirm it, and discovery refuses to run with zero confirmed signals.

```bash
mercury signals                       # review the catalog
mercury signals --confirm free        # confirm everything that costs nothing
mercury signals --confirm SERP_RANK   # a paid one, if you want rank data
mercury signals --reject BLOG_STALE   # turn one off
```

Or open the dashboard's Signals tab, where each signal has a description, its cost, and how many companies already carry it. See [Prospecting](prospecting.md#signals) for the model.

## 5. Run a first discovery

Discovery is the only stage that spends money, so it always estimates first.

```bash
mercury discover --providers          # every source, its cost, whether it is set up
mercury discover --estimate           # projected spend for the default (free) source
mercury discover                      # run OpenStreetMap: free, no account
```

Cities come from `icp.geography`, or pass `--city "Denver, CO;Boulder, CO"` (semicolon-separated). A discovery run chains straight into profiling, which reads each business's homepage, robots.txt and sitemap for free.

OpenStreetMap is a trial source. It maps premises, so it has thin coverage for trades without a storefront. For real volume see [Discovery providers](prospecting.md#discovery-providers).

## 6. Start the dashboard

```bash
mercury dashboard                     # http://127.0.0.1:5555
```

It opens on Today: anything waiting on a decision, then pipeline numbers. The [Dashboard guide](dashboard.md) covers every tab.

## 7. Start the loop

```bash
mercury run
```

Every 15 minutes (`usage.heartbeat_interval_minutes`) Mercury wakes up, checks quiet hours and its Claude budget, decides what to do, and does it. It verifies addresses, writes three-email sequences for verified prospects, stages them in the outbox, sends what you approved, and reads replies. Stop it with Ctrl+C; it finishes the current step first.

For a single cycle (cron, CI, a scheduled container), use `mercury run --once`. See [Deployment](deployment.md) and [Cloud runs](cloud.md).

## 8. Review the outbox

With a native provider and `require_approval: true` (the default), every outgoing email waits in the outbox. Open the dashboard's Outbox tab and work through the queue: <kbd>A</kbd> approves, <kbd>R</kbd> rejects, <kbd>J</kbd>/<kbd>K</kbd> move between drafts. You can edit a draft in place or ask the writer to regenerate it with an instruction.

From the terminal:

```bash
mercury outbox                        # list what is waiting
mercury outbox --approve <id>
mercury outbox --reject <id>          # also rejects later steps of that sequence
mercury outbox --approve-all
```

Approved emails send on schedule, at most 8 per cycle, with a few seconds of jitter between sends. Read the first few dozen carefully. Once you trust the output you can set `auto_approve_followups: true`, or `require_approval: false` for full autopilot. See [The approval outbox](email-and-deliverability.md#the-approval-outbox).

## Check status

```bash
mercury status                        # pipeline counts
mercury usage                         # Claude quota and Mercury's own token usage
mercury sending                       # is the kill switch on?
mercury export                        # deliverable prospects to prospects.csv
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| `command not found: mercury` | Activate the venv: `source .venv/bin/activate`. |
| `ModuleNotFoundError: No module named 'mercury'` after install (macOS) | Python 3.13 ignores `.pth` files carrying the macOS hidden flag, which some Macs propagate into `.venv`. Link the package into the venv: `ln -s "$(pwd)/mercury" .venv/lib/python3.13/site-packages/mercury`. `mercury install` applies the same fix automatically when it can run. |
| `externally-managed-environment` | You are using the system Python. Create and activate a venv first. |
| `Mercury needs Python 3.11 or newer` | Rebuild the venv from a newer interpreter, e.g. `brew install python@3.13`, then `rm -rf .venv && $(brew --prefix)/bin/python3.13 -m venv .venv`. |
| Brain calls fail, "not logged in" in the log | Run `claude login` as the same user that runs Mercury, on a Pro or Max plan. |
| `Configuration problem: ...` | The message names the field. Fix it in `mercury.local.yaml` (or whichever file is in use). |
| Outbox never sends, log says "compliance hold" | Set `compliance.postal_address`. |
| Outbox never sends, log says "not configured yet" | The provider's credentials are missing. Run `mercury mail test`. |
| Every prospect is `guess`, nothing is drafted | Add a verifier key (`REOON_API_KEY`, `ZEROBOUNCE_API_KEY` or `HUNTER_API_KEY`). |
| `mercury mail test` times out | The network blocks SMTP/IMAP ports (common on cloud runners). Use Gmail, which goes over HTTPS. |
| Discovery stops with "no confirmed signals" | Run `mercury signals --confirm free` or confirm signals on the Signals tab. |
| Web search is rate limited | Scout falls back from Serper to DuckDuckGo, Bing and Google scraping. A `SERPER_API_KEY` (2,500 free queries) makes search reliable. |
| SQLite errors on first run | `data/` is created automatically. Make sure the user running Mercury can write to the checkout. |
| Instantly API 401 (legacy provider) | Wrong key, or the account is below Instantly's Growth plan, which API access requires. |

More questions are answered in the [FAQ](faq.md).
