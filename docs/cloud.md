# Running Harvey on a schedule in the cloud

Harvey is built as a daemon: `harvey run` loops forever, sleeping between
heartbeats, keeping its pipeline in a SQLite file next to the checkout. That
shape assumes a machine that stays up and a disk that stays put.

A scheduled cloud run has neither. The container is created for one firing and
reclaimed afterwards, so the loop has nowhere to loop and the database has
nowhere to live. Two pieces bridge the gap.

## 1. One cycle per firing

```bash
harvey run --once                      # one cycle, then exit
harvey run --once --ignore-quiet-hours # ...even inside quiet hours
```

`--once` runs exactly one heartbeat — budget check, decision, agents, logging —
and returns an exit code. The scheduler owns the cadence; Harvey owns what
happens inside a cycle. The loop and the one-shot share `run_cycle()`, so the
two paths cannot drift apart.

Quiet hours still apply. A schedule that overlaps `usage.quiet_hours` no-ops
rather than emailing people at 3am, which keeps `harvey.yaml` the single source
of truth for when Harvey is allowed to be awake.

Exit codes: `0` a cycle ran (or was skipped for quiet hours), `1` the
configuration is unusable or the cycle crashed. `--once` never opens the
interactive setup wizard — there is no terminal to answer it.

## 2. A private state store

Everything Harvey learns lives in `data/harvey.db`: companies, prospects,
observations, conversations, the outbox. Without it a scheduled run rediscovers
the world from scratch every hour and re-emails people it already contacted.

**This repository is public, so prospect data must not go in it.** The state
store is a separate *private* repo, and `scripts/cloud_run.sh` treats it as the
real home of the deployment:

```
harvey-state/                        (private)
  data/harvey.db                     the pipeline
  data/gmail_token.json              the refreshed OAuth token
  harvey.local.yaml                  the trained config
  skills/product_knowledge.md        what Harvey is selling
  skills/competitive_intel.md
```

`harvey.local.yaml` takes precedence over the tracked `harvey.yaml` template
(see `config._find_config_file`), which is how a public checkout runs a private
business configuration without ever committing one.

Each firing: clone the state repo, symlink `data/` and the config files into
the checkout, run one cycle, checkpoint the WAL, commit and push. A failed push
loses that cycle's work but never corrupts the store — the next run resumes
from the last commit that landed.

## Setup

### a. Create the state repo

Create a **private** repo (e.g. `harvey-state`) on GitHub and give the Claude
GitHub App access to it, at
<https://github.com/apps/claude/installations/select_target>. It can be empty;
the script seeds its layout and `.gitignore` on first run.

### b. Put the trained config in it

Train locally, then move the generated files across:

```bash
harvey train https://your-product.com
mv harvey.yaml harvey.local.yaml        # keep the template tracked, the real one private
git -C ../harvey-state add harvey.local.yaml skills/ && git -C ../harvey-state commit -m "Config"
```

### c. Set environment variables

Harvey reads `os.environ` directly, so a cloud deployment needs no `.env` file
— set these in the environment's variable settings:

| Variable | Why |
|---|---|
| `HARVEY_STATE_REPO` | **Required.** `https://github.com/<you>/harvey-state.git` |
| `SERPER_API_KEY` | **Strongly recommended.** Google rate-limits datacenter IPs on the first request and DuckDuckGo serves a challenge, so free search is close to useless from a cloud container. Bing alone is thin. |
| `REOON_API_KEY` / `ZEROBOUNCE_API_KEY` / `HUNTER_API_KEY` | Email verification. Without one, every address stays `guess` and is never sent. |
| `DATAFORSEO_LOGIN` / `DATAFORSEO_PASSWORD` | Paid discovery. `DATAFORSEO_SANDBOX=1` routes to the free sandbox. |
| Mail provider | `GMAIL_CLIENT_ID`/`GMAIL_CLIENT_SECRET`, or the `SMTP_*`/`IMAP_*` set. |

### d. Run it

```bash
HARVEY_STATE_REPO=https://github.com/<you>/harvey-state.git scripts/cloud_run.sh
```

The script installs dependencies on first use, so the first firing is slower
than the rest.

## Approval still applies

With `channels.email.require_approval: true` (the default) a scheduled run
drafts and queues, but nothing leaves the building. Review the outbox by
cloning the state repo locally and pointing the dashboard at it:

```bash
git clone https://github.com/<you>/harvey-state.git
ln -s ../harvey-state/data data
harvey dashboard        # localhost:5555 → Outbox
harvey outbox --approve-all
```

Approvals commit back to the state repo and the next cloud firing sends them.

Only set `require_approval: false` once you have read enough of Harvey's output
to trust it unattended. A scheduled job sending cold email with nobody watching
puts your sending domain's reputation on the line every hour.

## Gmail in a headless container

`harvey gmail auth` opens a browser, which a container does not have. Run it
once locally, then commit the resulting `data/gmail_token.json` to the state
repo. The refresh token survives, and because `data/` is synced back after
every cycle, a refreshed token persists automatically.

SMTP+IMAP avoids the problem entirely — it is pure environment variables.

## Root containers and the Claude CLI

Harvey's brain shells out to `claude -p --dangerously-skip-permissions`. The
CLI refuses that flag when running as root unless `IS_SANDBOX` is set, and most
hosted runners are root. `brain._cli_env()` sets it automatically when the
effective uid is 0 — where the container *is* the sandbox. The project's own
Docker image runs as an unprivileged user and is unaffected.
