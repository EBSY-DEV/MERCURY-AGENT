"""The sending switches, for every interface.

An operator pause and a health hold are separate (mercury/holds.py has the
model). Resume lifts the operator pause and nothing else: a bounce kill
switch, its counters, paused mailboxes, suppression, caps and pacing all
stay as they were, and the result says which holds are still in force.
Lifting the kill switch is its own command (``clear_hold``), it needs the
admin scope, and it is the only path that restarts the bounce count.

``status`` is the one read every interface shows, so the dashboard and the
CLI list the same reasons.
"""

from __future__ import annotations

from mercury import holds

# What the switch says when a person flips it, by interface.
PAUSE_REASONS = {"dashboard": "paused from dashboard", "cli": "paused manually", "mcp": "paused from MCP"}


class SendingService:
    def __init__(self, ctx, state, config=None):
        # config is optional: without it the compliance hold is not reported
        # (the sender still enforces it).
        self.ctx, self.state, self.config = ctx, state, config

    async def ready(self):
        await self.state.init_db()
        return self

    async def status(self) -> dict:
        """``blocked``: no email will be claimed. ``paused``/``reason``: the
        operator pause alone. ``holds``: every policy hold, each with its
        ``scope`` (global holds block everything, a mailbox hold one inbox).
        ``in_flight``: rows claimed before any pause, still being sent."""
        self.ctx.require("read")
        reason = await holds.operator_pause(self.state)
        found = await holds.holds(self.state, self.config)
        return {
            "blocked": bool(reason) or any(h["scope"] == "global" for h in found),
            "paused": bool(reason),
            "reason": reason,
            "holds": found,
            "in_flight": await holds.in_flight(self.state),
            "bounce_counters": await holds.bounce_counters(self.state),
        }

    async def pause(self, reason: str = "") -> dict:
        """Nothing new is claimed until resumed. A pause already in place takes
        the new reason. An email already claimed finishes (see ``in_flight``)."""
        self.ctx.require("run")
        reason = " ".join((reason or "").split())[:200] or PAUSE_REASONS[self.ctx.client]
        await holds.set_operator_pause(self.state, reason)
        return await self.status()

    async def resume(self) -> dict:
        """Lift the operator pause only. Whatever else holds sending is
        returned in ``holds`` and ``blocked`` stays true while a global one
        remains. Repeating it changes nothing."""
        self.ctx.require("run")
        await holds.clear_operator_pause(self.state)
        return await self.status()

    async def clear_hold(self) -> dict:
        """Lift the bounce kill switch after a person has fixed its cause, and
        start a clean bounce count. Logged with the counters it reset. The
        operator pause and mailbox holds are untouched; with no kill switch
        on, nothing changes and the counters are kept."""
        self.ctx.require("admin")
        cleared = await holds.clear_health_hold(self.state)
        if cleared:
            await self.state.log_action(
                action_type="sending_hold_cleared",
                agent=self.ctx.actor,
                details={"operator": self.ctx.operator, **cleared},
            )
        return {**await self.status(), "cleared": cleared}
