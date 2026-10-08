"""The agent process: status, start, stop and its log, for every interface.

The PID file is the shared truth: an agent started from a terminal shows as
running here too. A child started from this process is also tracked by its
handle, because a child that has exited stays a zombie (and still answers
os.kill(pid, 0)) until it is polled.

Stopping is cooperative. ``stop`` sends SIGTERM once: the agent finishes the
step it is on (an email already claimed is sent and recorded, nothing new is
claimed) and exits. If it has not exited within the wait, ``stop`` reports
``stopping`` instead of killing it, and ``status`` keeps reporting it until
it is gone. A second SIGTERM would make the agent exit at once, so a repeated
``stop`` only reports progress; ``force=True`` is the explicit SIGKILL. A row
a kill leaves in ``sending`` is re-queued by the next cycle.
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
STOP_WAIT_SECONDS = 5.0

# The agent this process started, if any. Module level so every service
# instance in one process agrees about it.
_process: subprocess.Popen | None = None
_started_at: datetime | None = None


class RuntimeService:
    def __init__(self, ctx, project_root: Path, pid_file: Path, log_file: Path):
        self.ctx = ctx
        self.project_root, self.pid_file, self.log_file = Path(project_root), Path(pid_file), Path(log_file)
        # "<pid> <iso time>" while a requested stop is in progress. A file, so
        # the dashboard and the CLI agree on it like they do on the PID file.
        self.stop_file = self.pid_file.with_name(self.pid_file.name + ".stopping")

    @staticmethod
    def _alive(pid: int) -> bool:
        if _process and _process.pid == pid:
            return _process.poll() is None
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # it exists, even though this caller cannot signal it

    def _stop_requested(self, pid: int | None) -> str | None:
        """When a stop of ``pid`` was requested, or None. Clears a marker left
        by a process that is gone."""
        try:
            marked, at = self.stop_file.read_text().split(maxsplit=1)
        except (OSError, ValueError):
            return None
        if pid is None or str(pid) != marked:
            self.stop_file.unlink(missing_ok=True)
            return None
        return at.strip()

    def _stopped(self):
        global _process, _started_at
        _process = None
        _started_at = None
        self.pid_file.unlink(missing_ok=True)
        self.stop_file.unlink(missing_ok=True)

    def pid(self) -> int | None:
        """The running agent's pid, or None. Clears a stale PID file."""
        if _process and _process.poll() is None:
            return _process.pid
        if self.pid_file.exists():
            try:
                pid = int(self.pid_file.read_text().strip())
                os.kill(pid, 0)  # Check if process exists
                return pid
            except PermissionError:
                return pid
            except (ValueError, ProcessLookupError):
                self.pid_file.unlink(missing_ok=True)
        return None

    async def status(self) -> dict:
        self.ctx.require("read")
        pid = self.pid()
        started = _started_at.isoformat() if _started_at else None
        requested = self._stop_requested(pid)
        return {"running": pid is not None, "pid": pid, "started_at": started,
                "stopping": requested is not None, "stop_requested_at": requested}

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

    async def stop(self, wait_seconds: float | None = None, force: bool = False) -> dict:
        """Ask the agent to stop and wait up to ``wait_seconds`` (default
        STOP_WAIT_SECONDS) for it.

        Returns ``stopped`` (it has exited), ``stopping`` (it is finishing its
        current step), ``forced`` (it was killed) and ``waited`` (seconds).
        """
        self.ctx.require("run")
        pid = self.pid()
        if not pid:
            raise Conflict("Mercury is not running.", code="not_running")

        requested = self._stop_requested(pid)
        if requested is None:
            # Persist before signalling: without a marker another stop
            # could send a second SIGTERM and interrupt an in-flight email.
            requested = datetime.now().isoformat(timespec="seconds")
            try:
                self.stop_file.write_text(f"{pid} {requested}")
            except OSError as e:
                raise Unavailable(f"Cannot record the stop request: {e}",
                                  code="stop_failed") from e
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                self.stop_file.unlink(missing_ok=True)
                raise Unavailable(f"Cannot signal Mercury (pid {pid}): {e}",
                                  code="stop_failed") from e

        wait_seconds = STOP_WAIT_SECONDS if wait_seconds is None else wait_seconds
        waited = 0.0
        step = 0.25
        while self._alive(pid) and waited < wait_seconds:
            await asyncio.sleep(step)
            waited += step

        forced = False
        if self._alive(pid):
            if not force:
                return {"pid": pid, "stopped": False, "stopping": True, "forced": False,
                        "stop_requested_at": requested, "waited": waited}
            forced = True
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError as e:
                raise Unavailable(f"Cannot force stop Mercury (pid {pid}): {e}",
                                  code="stop_failed") from e
            # A successful signal is not proof of exit, especially for a
            # process stuck in kernel I/O. Keep tracking it until it is gone.
            force_waited = 0.0
            while self._alive(pid) and force_waited < 2.0:
                await asyncio.sleep(step)
                force_waited += step
                waited += step
            if self._alive(pid):
                return {"pid": pid, "stopped": False, "stopping": True, "forced": True,
                        "stop_requested_at": requested, "waited": waited}

        self._stopped()
        return {"pid": pid, "stopped": True, "stopping": False, "forced": forced,
                "stop_requested_at": requested, "waited": waited}

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
