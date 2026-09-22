#!/usr/bin/env bash
#
# Review and approve Harvey's outbox on your own machine.
#
# The scheduled cloud run drafts email and queues it; nothing is sent until a
# human approves it. That approval happens here: pull the state repo, point the
# dashboard at it, approve what looks good, and push the decisions back so the
# next cloud cycle sends them.
#
#   HARVEY_STATE_REPO=https://github.com/<you>/harvey-state.git scripts/local_dashboard.sh
#
# Run it from a normal clone of this repository. Ctrl-C when done; approvals are
# pushed back on exit.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

STATE_DIR="$ROOT/.harvey-state"
: "${HARVEY_STATE_REPO:?set HARVEY_STATE_REPO to the private state repo URL}"

log() { printf '[dashboard] %s\n' "$*" >&2; }

push_state() {
    if [ ! -d "$STATE_DIR/.git" ]; then
        return
    fi
    # Checkpoint the WAL so the committed database includes this session's
    # approvals rather than leaving them in a sidecar file.
    if [ -f "$STATE_DIR/data/harvey.db" ] && [ -x "$ROOT/.venv/bin/python" ]; then
        "$ROOT/.venv/bin/python" - "$STATE_DIR/data/harvey.db" <<'PY' || true
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
conn.close()
PY
    fi
    cd "$STATE_DIR"
    git add -A
    if git diff --cached --quiet; then
        log "no changes to push"
    else
        git -c user.name="Harvey" -c user.email="harvey@users.noreply.github.com" \
            commit --quiet -m "Review session $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        if git push --quiet origin HEAD; then
            log "approvals pushed; the next cloud cycle will act on them"
        else
            log "PUSH FAILED -- your approvals are committed locally in $STATE_DIR"
        fi
    fi
}
trap push_state EXIT

# --- state in --------------------------------------------------------------
if [ -d "$STATE_DIR/.git" ]; then
    log "refreshing state"
    git -C "$STATE_DIR" pull --ff-only --quiet
else
    log "cloning state repo"
    git clone --depth 1 --quiet "$HARVEY_STATE_REPO" "$STATE_DIR"
fi

mkdir -p "$STATE_DIR/data" "$STATE_DIR/skills"
rm -rf "$ROOT/data"
ln -sfn "$STATE_DIR/data" "$ROOT/data"
for f in harvey.local.yaml skills/product_knowledge.md skills/competitive_intel.md; do
    if [ -f "$STATE_DIR/$f" ]; then
        ln -sfn "$STATE_DIR/$f" "$ROOT/$f"
    fi
done

# --- dependencies ----------------------------------------------------------
if [ ! -x "$ROOT/.venv/bin/harvey" ]; then
    log "installing dependencies"
    python3 -m venv .venv
    ./.venv/bin/pip install --quiet --upgrade pip
    ./.venv/bin/pip install --quiet -e .
fi

# --- dashboard -------------------------------------------------------------
log "starting dashboard on http://localhost:5555  (Ctrl-C to stop and push)"
./.venv/bin/harvey dashboard
