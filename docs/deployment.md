# Deployment

`mercury run` is a long-running loop, so for real use you want it on a machine that stays up: a home server, a small VPS, or a Docker host. This page shows systemd units for the agent and the dashboard, the Docker setup that ships with the repo, single-cycle scheduled runs, backups, upgrades (including from the project's previous name, Harvey), and where to find logs.

Whatever you choose, the `claude` CLI must be installed and logged in (`claude login`) as the user that runs Mercury, and that user must be able to write to the checkout (`data/` holds the database).

## systemd

Two units: the heartbeat, and the dashboard bound to a private address. Replace `mercury` (user), `/home/mercury/mercury` (checkout) and the IP with your own.

`/etc/systemd/system/mercury.service`:

```ini
[Unit]
Description=Mercury Agent (heartbeat)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mercury
WorkingDirectory=/home/mercury/mercury
# The claude CLI usually lives in ~/.local/bin, which systemd's default PATH lacks.
Environment=PATH=/home/mercury/.local/bin:/home/mercury/mercury/.venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
ExecStart=/home/mercury/mercury/.venv/bin/mercury run
Restart=on-failure
RestartSec=30
# SIGTERM lets the current step finish; give a long Claude call time to return.
TimeoutStopSec=330

[Install]
WantedBy=multi-user.target
```

`/etc/systemd/system/mercury-dashboard.service`:

```ini
[Unit]
Description=Mercury dashboard
After=network-online.target tailscaled.service
Wants=network-online.target

[Service]
Type=simple
User=mercury
WorkingDirectory=/home/mercury/mercury
Environment=PATH=/home/mercury/.local/bin:/home/mercury/mercury/.venv/bin:/usr/local/bin:/usr/bin:/bin
# Bind to the tailnet address only. Never 0.0.0.0 on a public host: there is no login.
ExecStart=/home/mercury/mercury/.venv/bin/mercury dashboard --host 100.101.102.103 --port 5555
# Retry until the tailnet interface is up.
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mercury mercury-dashboard
systemctl status mercury
journalctl -u mercury -f
```

A few things to know:

- `mercury run` opens the interactive setup wizard if it finds no credentials or the config still says `company: "Your Company"`. Under systemd there is nobody to answer it, so configure and test (`mercury mail test`, `mercury run --once`) interactively first.
- The service reads `.env` from the checkout and the config from `mercury.local.yaml` (or `MERCURY_CONFIG`, which you can set with another `Environment=` line).
- Mercury handles SIGTERM gracefully: the current cycle's work finishes, then it exits. A second signal exits immediately.
- Gmail's refresh token lives in `data/gmail_token.json` and refreshes itself; run `mercury gmail auth` once, interactively, before starting the service.
- The dashboard's security model is covered in [Dashboard](dashboard.md#running-it-on-another-machine).

## Docker

The repo ships a `Dockerfile` and `docker-compose.yml` for the heartbeat.

What the image does: Python 3.12 slim, the Python dependencies from `requirements.txt`, Chromium for Playwright, a non-root user `mercury` (uid 1000), and the Claude Code CLI installed for that user. Its command is `python -m mercury.main` (the heartbeat). A health check marks the container unhealthy when `data/mercury.db` has not been written for two hours.

What `docker-compose.yml` mounts:

| Host | Container | Why |
|---|---|---|
| `.env` (as `env_file`) | environment | Credentials. |
| `./data` | `/app/data` | The database and Gmail token survive restarts. |
| `./mercury.yaml` | `/app/mercury.yaml` (read-only) | Config. |
| `./prompts` | `/app/prompts` (read-only) | Edit prompts without rebuilding. |
| `./skills` | `/app/skills` | Skills, including the generated product knowledge. |
| `~/.claude` | `/home/mercury/.claude` | Claude login. Writable, because the CLI refreshes its OAuth token in place. |

```bash
docker compose up -d --build
docker compose logs -f mercury
```

Adjustments you will probably need:

- **`mercury.local.yaml` is not mounted.** `mercury train` writes `mercury.local.yaml`, and the container would otherwise see only the template (and try to open the setup wizard). Add a volume line `- ./mercury.local.yaml:/app/mercury.local.yaml:ro`, or set `MERCURY_CONFIG` to a mounted path.
- **Host `~/.claude` ownership.** The container runs as uid 1000. If your host user has a different uid, the CLI inside cannot read or refresh the mounted credentials.
- **No dashboard service.** To add one, give it the same build, volumes and `env_file`, plus:

  ```yaml
    dashboard:
      build: .
      env_file: .env
      command: ["python", "-m", "mercury", "dashboard", "--host", "0.0.0.0", "--port", "5555"]
      ports:
        - "127.0.0.1:5555:5555"   # host loopback only; tunnel or use a tailnet to reach it
      volumes:
        - ./data:/app/data
        - ./mercury.yaml:/app/mercury.yaml:ro
        - ./mercury.local.yaml:/app/mercury.local.yaml:ro
      restart: unless-stopped
  ```

  `0.0.0.0` inside the container is fine here because the published port is bound to the host's loopback. The image installs dependencies but not the `mercury` console script, so use `python -m mercury`.

- **Mail ports.** SMTP and IMAP need outbound 587/465 and 993. Some hosts block them; `docker compose exec mercury python -m mercury mail test` will tell you.

## Scheduled single-cycle runs

If you don't have a machine that stays up, run one cycle at a time from a scheduler:

```bash
mercury run --once                        # one cycle, then exit
mercury run --once --ignore-quiet-hours   # even during quiet hours
```

Exit code `0` means a cycle ran or was skipped for quiet hours; `1` means the configuration is unusable or the cycle crashed. `--once` never opens the setup wizard.

A crontab entry every 15 minutes:

```cron
*/15 * * * * cd /home/mercury/mercury && PATH=$HOME/.local/bin:$PATH .venv/bin/mercury run --once >> data/cron.log 2>&1
```

For ephemeral containers (CI runners, scheduled cloud jobs) the database has to live somewhere between runs. [Cloud runs](cloud.md) covers `scripts/cloud_run.sh`, which keeps state in a private git repository.

Note that a one-shot run does not continue a daily background discovery that was still running when the process exits. Long discovery runs belong in `mercury discover` or a long-running loop.

## Backups

Everything Mercury knows is in `data/mercury.db` (SQLite in WAL mode). Copying the file while Mercury writes can produce an inconsistent copy, because recent writes may still sit in `mercury.db-wal`. Use SQLite's own tools instead.

A consistent snapshot of a live database:

```bash
mkdir -p backups
sqlite3 data/mercury.db "VACUUM INTO 'backups/mercury-$(date +%F).db'"
```

Without the `sqlite3` CLI:

```bash
.venv/bin/python -c "import sqlite3,sys; sqlite3.connect('data/mercury.db').execute(\"VACUUM INTO ?\", (sys.argv[1],))" backups/mercury-$(date +%F).db
```

If you must copy the files directly, checkpoint the WAL first, ideally with Mercury stopped:

```bash
sqlite3 data/mercury.db "PRAGMA wal_checkpoint(TRUNCATE);"
cp data/mercury.db backups/
```

Also back up the files that are gitignored and not in the database:

- `.env`
- `mercury.local.yaml`
- `data/gmail_token.json`
- `skills/product_knowledge.md` and `skills/competitive_intel.md`

To restore, stop Mercury and the dashboard, put the snapshot at `data/mercury.db`, delete any leftover `data/mercury.db-wal` and `data/mercury.db-shm`, and start again.

## Upgrading

```bash
sudo systemctl stop mercury mercury-dashboard     # or stop however you run it
sqlite3 data/mercury.db "VACUUM INTO 'backups/pre-upgrade.db'"
git pull
source .venv/bin/activate
pip install -e .
sudo systemctl start mercury mercury-dashboard
```

Database migrations are automatic. The schema version is stored in SQLite's `PRAGMA user_version`; on startup Mercury applies any pending migrations in a single transaction, so a failed upgrade rolls back rather than leaving a half-migrated database. New signals shipped in an upgrade appear on the Signals tab as `proposed`. Read `CHANGELOG.md` for config keys that changed.

### Upgrading from Harvey

Mercury used to be called Harvey. An existing checkout upgrades in place:

- **Files are renamed automatically** on first start, without overwriting anything that already exists: `harvey.local.yaml` to `mercury.local.yaml`, `data/harvey.db` (with its `-wal` and `-shm` files) to `data/mercury.db`, and `data/harvey.log` to `data/mercury.log`. The cloud scripts do the same inside the state repo, and still read `HARVEY_STATE_REPO` when `MERCURY_STATE_REPO` is unset.
- **The Python package was renamed** from `harvey` to `mercury`. Remove the old distribution before installing, so its stale entry point and metadata don't linger:

  ```bash
  source .venv/bin/activate
  pip uninstall -y harvey
  pip install -e .
  ```

- **The `harvey` command still works** as a deprecated alias for `mercury`, so existing units running `.venv/bin/harvey run` keep working. Switch them to `mercury` when convenient; the alias will be removed eventually.

## Logs

| How Mercury runs | Where logs go |
|---|---|
| `mercury run` in a terminal | stdout |
| systemd | the journal: `journalctl -u mercury -f` |
| Docker | `docker compose logs -f mercury` (json-file driver, 3 x 10 MB rotation) |
| Started from the dashboard's Controls tab | `data/mercury.log`, shown on that tab |
| cron | wherever you redirect it |

Each line looks like `2026-10-06 09:15:02 [mercury.sender] INFO: Sender: sent step 1 to ...`. Useful things to grep for: `Decision:` (what each cycle chose), `compliance hold`, `SENDING PAUSED`, `KILL SWITCH`, `Warm-up:`, `BOUNCE`, `Quota gate`.

HTTP request URLs from the `httpx` library are suppressed at INFO, because some verifier APIs take the key as a query parameter and it would otherwise end up in your logs.

Everything Mercury does is also recorded in the `actions` table, which the Activity tab shows.
