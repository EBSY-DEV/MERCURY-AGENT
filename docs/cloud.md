# Running Mercury on a schedule in the cloud

Mercury is built as a daemon: `mercury run` loops forever, sleeping between
heartbeats, keeping its pipeline in a SQLite file next to the checkout. That
shape assumes a machine that stays up and a disk that stays put.

A scheduled cloud run has neither. The container is created for one firing and
reclaimed afterwards, so the loop has nowhere to loop and the database has
nowhere to live. Two pieces bridge the gap.

If you do have a machine that stays up, [Deployment](deployment.md) (systemd,
Docker, cron) is simpler.

## 1. One cycle per firing

```bash
mercury run --once                      # one cycle, then exit
mercury run --once --ignore-quiet-hours # ...even inside quiet hours
```

`--once` runs exactly one heartbeat — budget check, decision, agents, logging —
and returns an exit code. The scheduler owns the cadence; Mercury owns what
happens inside a cycle. The loop and the one-shot share `run_cycle()`, so the
two paths cannot drift apart.

Quiet hours still apply. A schedule that overlaps `usage.quiet_hours` no-ops
rather than emailing people at 3am, which keeps `mercury.yaml` the single source
of truth for when Mercury is allowed to be awake.

Exit codes: `0` a cycle ran (or was skipped for quiet hours), `1` the
configuration is unusable or the cycle crashed. `--once` never opens the
interactive setup wizard — there is no terminal to answer it.

## 2. A private state store

Everything Mercury learns lives in `data/mercury.db`: companies, prospects,
observations, conversations, the outbox. Without it a scheduled run rediscovers
the world from scratch every hour and re-emails people it already contacted.

**This repository is public, so prospect data must not go in it.** The state
store is a separate *private* repo, and `scripts/cloud_run.sh` treats it as the
real home of the deployment:

```
mercury-state/                        (private)
  data/mercury.db                     the pipeline
  data/gmail_token.json              the refreshed OAuth token
  mercury.local.yaml                  the trained config
  skills/product_knowledge.md        what Mercury is selling
  skills/competitive_intel.md
```

`mercury.local.yaml` takes precedence over the tracked `mercury.yaml` template
(see `config._find_config_file`), which is how a public checkout runs a private
business configuration without ever committing one.

A state repo created before the Harvey → Mercury rename (with `harvey.local.yaml`
and `data/harvey.db`) needs no manual step: both scripts rename those files
inside the checkout on the next run, and the rename is pushed with it.

Each firing: clone the state repo, symlink `data/` and the config files into
the checkout, run one cycle, checkpoint the WAL, commit and push. A failed push
loses that cycle's work but never corrupts the store — the next run resumes
from the last commit that landed.

## Setup

### a. Create the state repo

Create a **private** repo (e.g. `mercury-state`) on GitHub and give the Claude
GitHub App access to it, at
<https://github.com/apps/claude/installations/select_target>. It can be empty;
the script seeds its layout and `.gitignore` on first run.

### b. Put the trained config in it

Train locally (it writes `mercury.local.yaml` and the product skills, all
gitignored here), review the config, then copy the generated files across:

```bash
mercury train https://your-product.com
mkdir -p ../mercury-state/skills
cp mercury.local.yaml ../mercury-state/
cp skills/product_knowledge.md ../mercury-state/skills/
cp skills/competitive_intel.md ../mercury-state/skills/   # only exists if competitors were found
git -C ../mercury-state add mercury.local.yaml skills/ && git -C ../mercury-state commit -m "Config"
```

Set `channels.email.provider` and `compliance.postal_address` before you copy
it: the trainer writes `provider: instantly` and no postal address, and the
native sender holds the outbox until the address is set (see
[Configuration](configuration.md)).

### c. Set environment variables

Mercury reads `os.environ` directly, so a cloud deployment needs no `.env` file
— set these in the environment's variable settings:

| Variable | Why |
|---|---|
| `MERCURY_STATE_REPO` | **Required.** `https://github.com/<you>/mercury-state.git` (the old name `HARVEY_STATE_REPO` is still read when this is unset) |
| `SERPER_API_KEY` | **Strongly recommended.** Google rate-limits datacenter IPs on the first request and DuckDuckGo serves a challenge, so free search is close to useless from a cloud container. Bing alone is thin. |
| `REOON_API_KEY` / `ZEROBOUNCE_API_KEY` / `HUNTER_API_KEY` | Email verification. Without one, every address stays `guess` and is never sent. |
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | Paid discovery. `DATAFORSEO_SANDBOX=1` routes to the free sandbox. |
| Mail provider | `GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET`, or the `SMTP_*`/`IMAP_*` set. Verify either with `mercury mail test`. |

### d. Run it

```bash
MERCURY_STATE_REPO=https://github.com/<you>/mercury-state.git scripts/cloud_run.sh
```

The script installs dependencies on first use, so the first firing is slower
than the rest.

## Approval still applies

With `channels.email.require_approval: true` (the default) a scheduled run
drafts and queues, but nothing leaves the building. Review it from your own machine:

```bash
MERCURY_STATE_REPO=https://github.com/<you>/mercury-state.git scripts/local_dashboard.sh
```

That pulls the state repo, points the dashboard at it (localhost:5555 → Outbox),
and pushes your approvals back when you stop it, so the next cloud firing sends
what you approved. `mercury outbox --approve-all` does the same from the terminal.

The dashboard is a local web UI. It cannot be reached from the scheduled cloud
container, which has no exposed ports and is destroyed after each run — the
state repo is what carries decisions between the two.

Only set `require_approval: false` once you have read enough of Mercury's output
to trust it unattended. A scheduled job sending cold email with nobody watching
puts your sending domain's reputation on the line every hour.

## Choosing a provider: check the egress first

**A scheduled cloud container may only be allowed to talk HTTPS.** Measured on
the Anthropic cloud environment:

| Port | Purpose | Result |
|---|---|---|
| 443 | HTTPS | open |
| 587 | SMTP submission | blocked |
| 465 | SMTP implicit TLS | blocked |
| 993 | IMAP over TLS | blocked |
| 25 | SMTP relay | blocked |

That decides the provider, and it is the opposite of the answer you get by
reasoning about convenience alone:

- **`gmail` works.** It is the Gmail REST API over HTTPS
  (`gmail.googleapis.com`), so it goes through the same egress as everything
  else. The OAuth browser step is a one-time annoyance run locally; the token
  then lives in the state repo and refreshes itself.
- **`smtp` cannot send or read mail from such a container.** It needs raw TCP
  on 587/465 to send and 993 to poll replies. `mercury mail test` reports this
  as a connection timeout, not an auth error.

SMTP remains the better choice when Mercury runs somewhere with open mail ports
— a laptop, a VPS, a Docker host. It is simpler: pure environment variables, no
OAuth, `IMAP_USERNAME`/`IMAP_PASSWORD` falling back to their SMTP equivalents,
and STARTTLS or implicit TLS picked automatically from the port.

Run `mercury mail test` on the machine that will actually do the sending, before
trusting either. A timeout there means ports, not credentials.

Whichever you choose, what governs whether mail *arrives* is the sending domain,
not the provider: a dedicated domain, correct SPF/DKIM/DMARC, verified
addresses, and a slow warmup.

## Gmail in a headless container

`mercury gmail auth` opens a browser, which a container does not have. Run it
once locally, then commit the resulting `data/gmail_token.json` to the state
repo. The refresh token survives, and because `data/` is synced back after
every cycle, a refreshed token persists automatically.

SMTP+IMAP avoids the problem entirely — it is pure environment variables.

## Root containers and the Claude CLI

Mercury's brain shells out to `claude -p --dangerously-skip-permissions`. The
CLI refuses that flag when running as root unless `IS_SANDBOX` is set, and most
hosted runners are root. `brain._cli_env()` sets it automatically when the
effective uid is 0 — where the container *is* the sandbox. The project's own
Docker image runs as an unprivileged user and is unaffected.
