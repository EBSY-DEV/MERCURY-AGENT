"""The agent process: status, start, stop and its log, for every interface.

The PID file is the shared truth: an agent started from a terminal shows as
running here too. A child started from this process is also tracked by its
handle, because a child that has exited stays a zombie (and still answers
os.kill(pid, 0)) until it is polled.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from mercury.control.errors import Conflict, Unavailable

logger = logging.getLogger(__name__)

LOG_TAIL_BYTES = 65536
LOG_LINES = 100

# The agent this process started, if any. Module level so every service
# instance in one process agrees about it.
_process: subprocess.Popen | None = None
_started_at: datetime | None = None


class RuntimeService:
    def __init__(self, ctx, project_root: Path, pid_file: Path, log_file: Path):
        self.ctx = ctx
        self.project_root, self.pid_file, self.log_file = Path(project_root), Path(pid_file), Path(log_file)

    def pid(self) -> int | None:
        """The running agent's pid, or None. Clears a stale PID file."""
        if _process and _process.poll() is None:
            return _process.pid
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
                return pid
            except (ValueError, ProcessLookupError, PermissionError):
                self.pid_file.unlink(missing_ok=True)
        return None

    async def status(self) -> dict:
        self.ctx.require("read")
        pid = self.pid()
        started = _started_at.isoformat() if _started_at else None
        return {"running": pid is not None, "pid": pid, "started_at": started}

    async def start(self) -> dict:
        """Start the heartbeat loop as a detached subprocess."""
        global _process, _started_at
        self.ctx.require("run")
        if self.pid():
            raise Conflict("Mercury is already running.", code="already_running")

        (self.project_root / "data").mkdir(parents=True, exist_ok=True)
        try:
            log_handle = open(self.log_file, "a")
            try:
                _process = subprocess.Popen(
                    [sys.executable, "-m", "mercury", "run"],
                    cwd=str(self.project_root),
                    stdout=log_handle,
                    stderr=log_handle,
                    start_new_session=True,
                )
            finally:
                # Child holds its own copies of the fds; don't leak ours.
                log_handle.close()
        except Exception as e:
            logger.warning("Failed to start Mercury: %s", e)
            raise Unavailable(f"Failed to start Mercury: {e}", code="start_failed") from e
        _started_at = datetime.now()

        try:
            self.pid_file.write_text(str(_process.pid))
        except OSError as e:
            logger.warning("Could not write PID file: %s", e)
        return {"pid": _process.pid}

    async def stop(self) -> dict:
        """SIGTERM, wait up to five seconds for a clean exit, then SIGKILL."""
        global _process, _started_at
        self.ctx.require("run")
        pid = self.pid()
        if not pid:
            raise Conflict("Mercury is not running.", code="not_running")

        forced = False
        try:
            os.kill(pid, signal.SIGTERM)
            for _ in range(10):
                try:
                    os.kill(pid, 0)
                    await asyncio.sleep(0.5)
                except ProcessLookupError:
                    break
            else:
                forced = True
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        except (ProcessLookupError, PermissionError):
            pass

        _process = None
        _started_at = None
        self.pid_file.unlink(missing_ok=True)
        return {"pid": pid, "forced": forced}

    async def logs(self, lines: int = LOG_LINES) -> dict:
        """The last lines of the agent log. Reads only the tail, so a huge
        log file never blocks the caller."""
        self.ctx.require("read")
        if not self.log_file.exists():
            return {"lines": []}
        try:
            with open(self.log_file, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                f.seek(max(0, size - LOG_TAIL_BYTES))
                text = f.read().decode("utf-8", errors="replace")
            return {"lines": text.strip().splitlines()[-lines:]}
        except Exception:
            return {"lines": []}
