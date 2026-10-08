"""The global sending switch, for every interface.

This wraps today's single switch: one ``sending_paused`` setting that both
the operator and the bounce monitor set, and that resume clears together
with the bounce counters. Separating an operator pause from a health hold
changes these semantics; callers already go through here when it does.
"""

from __future__ import annotations

from mercury.bounces import KILL_SWITCH_KEY

# What the switch says when a person flips it, by interface.
PAUSE_REASONS = {"dashboard": "paused from dashboard", "cli": "paused manually", "mcp": "paused from MCP"}


class SendingService:
    def __init__(self, ctx, state):
        self.ctx, self.state = ctx, state

    async def ready(self):
        await self.state.init_db()
        return self

    async def status(self) -> dict:
        self.ctx.require("read")
        reason = await self.state.get_setting(KILL_SWITCH_KEY)
        return {"paused": bool(reason), "reason": reason}

    async def pause(self, reason: str = "") -> dict:
        """Nothing leaves the outbox until resumed. A pause already in place
        takes the new reason."""
        self.ctx.require("run")
        reason = " ".join((reason or "").split())[:200] or PAUSE_REASONS[self.ctx.client]
        await self.state.set_setting(KILL_SWITCH_KEY, reason)
        return await self.status()

    async def resume(self) -> dict:
        """Clear the switch and start a clean bounce count."""
        self.ctx.require("run")
        from mercury.bounces import reset_counters

        await self.state.set_setting(KILL_SWITCH_KEY, "")
        await reset_counters(self.state)
        return await self.status()
