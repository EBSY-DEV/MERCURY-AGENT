#!/usr/bin/env bash
#
# One Harvey cycle inside an ephemeral container.
#
# The container is thrown away after every run, so the only thing that makes a
# schedule meaningful is the state store: a PRIVATE git repo holding data/
# (the SQLite pipeline, the Gmail token) plus the trained config that must
# never land in this public repository. Restore it, run one cycle, push it
# back. If the push fails the cycle is lost, not corrupted -- the next run
# simply picks up from the last commit that made it.
#
#   HARVEY_STATE_REPO   (required) https://github.com/<you>/harvey-state.git
#   everything else     API keys, injected as ordinary environment variables;
#                       Harvey reads os.environ, so no .env file is needed.
#
# Extra arguments are passed through to `harvey run --once`.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE_DIR="$ROOT/.harvey-state"
: "${HARVEY_STATE_REPO:?set HARVEY_STATE_REPO to the private state repo URL}"

log() { printf '[cloud-run] %s\n' "$*" >&2; }

# --- 1. State in -----------------------------------------------------------
if [ -d "$STATE_DIR/.git" ]; then
    log "refreshing state checkout"
    git -C "$STATE_DIR" pull --ff-only --quiet
else
    log "cloning state repo"
    git clone --depth 1 --quiet "$HARVEY_STATE_REPO" "$STATE_DIR"
fi

mkdir -p "$STATE_DIR/data" "$STATE_DIR/skills"

# Seed the state repo's ignore rules on first use. The WAL sidecars are noise
# at best -- committing harvey.db-wal on its own would capture the database
# mid-write -- and the pid/log files are per-container scratch.
if [ ! -f "$STATE_DIR/.gitignore" ]; then
    cat > "$STATE_DIR/.gitignore" <<'IGNORE'
data/*.db-wal
data/*.db-shm
data/harvey.pid
data/harvey.log
IGNORE
fi

# data/ is a symlink into the state checkout, so every write Harvey makes is
# already staged where the push will find it -- no copy-back step to forget.
rm -rf "$ROOT/data"
ln -sfn "$STATE_DIR/data" "$ROOT/data"

# The trained config lives in the state repo too. harvey.local.yaml wins over
# the tracked template (see config._find_config_file), which is how a public
# checkout runs a private business configuration.
for f in harvey.local.yaml skills/product_knowledge.md skills/competitive_intel.md; do
    if [ -f "$STATE_DIR/$f" ]; then
        ln -sfn "$STATE_DIR/$f" "$ROOT/$f"
    fi
done

# --- 2. Dependencies -------------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/harvey" ]; then
    log "installing dependencies"
    python3 -m venv .venv
    ./.venv/bin/pip install --quiet --upgrade pip
    ./.venv/bin/pip install --quiet -e .
fi

# --- 3. One cycle ----------------------------------------------------------
log "running one cycle"
set +e
./.venv/bin/harvey run --once "$@"
CYCLE_RC=$?
set -e
log "cycle exited $CYCLE_RC"

# --- 4. State out ----------------------------------------------------------
# Checkpoint the WAL first. SQLite holds recent writes in harvey.db-wal, and
# committing the database without it would push a file missing the very cycle
# that just ran.
if [ -f "$STATE_DIR/data/harvey.db" ]; then
    ./.venv/bin/python - "$STATE_DIR/data/harvey.db" <<'PY'
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.close()
PY
fi

cd "$STATE_DIR"
git add -A
if git diff --cached --quiet; then
    log "no state changes to push"
else
    git -c user.name="Harvey" \
        -c user.email="harvey@users.noreply.github.com" \
        commit --quiet -m "Cycle $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    pushed=0
    for attempt in 1 2 3 4; do
        if git push --quiet origin HEAD; then
            pushed=1
            break
        fi
        log "push failed, retrying in $((2 ** attempt))s"
        sleep $((2 ** attempt))
    done
    if [ "$pushed" -eq 1 ]; then
        log "state pushed"
    else
        log "STATE PUSH FAILED -- this cycle's work is not durable"
        exit 1
    fi
fi

exit "$CYCLE_RC"
