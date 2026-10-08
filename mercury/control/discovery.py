"""Discovery commands for the dashboard, the CLI and MCP.

Discovery is the only stage that spends money, so every interface goes
through ``plan`` first: it validates the provider, builds the exact query
list and prices it without calling anything. ``submit`` starts a run in the
background and returns at once; ``run`` awaits one (the CLI prints as it
goes). One background job at a time per process, held in memory: a restart
forgets it, and the run log (``mercury runs``) keeps what it did.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from mercury.control.errors import Conflict, Invalid

logger = logging.getLogger(__name__)

# The background job this process is running, and what the last one did.
_task: asyncio.Task | None = None
_report: dict | None = None


def running() -> bool:
    return bool(_task and not _task.done())


@dataclass
class DiscoveryPlan:
    provider: str
    queries: list
    estimated_cost: float
    free: bool

    def as_dict(self) -> dict:
        return {
            "provider": self.provider,
            "queries": [q.keyword() for q in self.queries],
            "query_count": len(self.queries),
            "estimated_cost": round(self.estimated_cost, 4),
            "free": self.free,
        }


class DiscoveryService:
    def __init__(self, ctx, state, config=None, env=None):
        # config/env None: read mercury.yaml and .env when first needed.
        self.ctx, self.state, self._config, self._env = ctx, state, config, env

    async def ready(self):
        await self.state.init_db()
        return self

    @property
    def config(self):
        if self._config is None:
            from mercury.config import load_config
            self._config = load_config()
        return self._config

    def menu(self) -> list[dict]:
        """What each source does, what it costs, and whether its keys are set.
        Reads .env only; no database."""
        self.ctx.require("read")
        from mercury.collectors.discover import provider_menu
        from mercury.config import load_env

        return provider_menu((self._env or load_env()).model_dump())

    async def providers(self) -> dict:
        """The menu plus the last choice, the kill switch and the job."""
        from mercury.collectors.discover import DEFAULT_PROVIDER

        return {
            "providers": self.menu(),
            "default": DEFAULT_PROVIDER,
            "selected": await self.state.get_setting("discovery_provider") or DEFAULT_PROVIDER,
            "paused": await self.state.get_setting("discovery_paused"),
            **self.job(),
        }

    def job(self) -> dict:
        return {"running": running(), "last_report": _report}

    def plan(self, provider: str, cities: list[str] | None = None,
             depth: int = 30, limit: int = 100) -> DiscoveryPlan:
        """The exact queries a run would make and what they would cost."""
        self.ctx.require("read")
        from mercury.collectors.discover import PROVIDERS, build_queries, estimate_cost

        if provider not in PROVIDERS:
            raise Invalid(f"unknown provider {provider!r}", code="unknown_provider")
        cities = [c.strip() for c in (cities or []) if c.strip()] or None
        queries = build_queries(self.config, cities=cities, depth=depth, limit=limit)
        return DiscoveryPlan(provider, queries, estimate_cost(provider, queries),
                             PROVIDERS[provider].estimate(queries) == 0)

    async def estimate(self, provider: str, cities: list[str] | None = None,
                       depth: int = 30, limit: int = 100) -> dict:
        return self.plan(provider, cities, depth, limit).as_dict()

    async def run(self, plan: DiscoveryPlan, max_spend: float = 1.0, profile: bool = True):
        """Discover, then (by default) read the sites that turned up. Awaits
        the whole run and returns its PipelineReport."""
        self.ctx.require("run")
        from mercury.pipeline import run_prospecting

        return await run_prospecting(self.state, self.config, plan.provider, plan.queries,
                                     max_spend=max_spend, profile=profile)

    def _start(self, work) -> None:
        global _task, _report

        async def _go():
            global _report
            try:
                _report = await work()
            except Exception as exc:
                logger.exception("discovery job failed")
                _report = {"errors": [str(exc)], "stopped": "failed"}

        _report = None
        _task = asyncio.create_task(_go())

    async def submit(self, provider: str, cities: list[str] | None = None, depth: int = 30,
                     limit: int = 100, max_spend: float = 1.0) -> dict:
        """Start a discovery run in the background and return at once."""
        self.ctx.require("run")
        if running():
            raise Conflict("a run is already going", code="job_running")
        plan = self.plan(provider, cities, depth, limit)
        await self.state.set_setting("discovery_provider", provider)
        await self.state.set_setting("discovery_paused", "")

        async def work():
            # Discovery chains straight into profiling: reading the sites
            # is free, and it is what makes the results worth anything.
            result = await self.run(plan, max_spend=max_spend)
            return {
                **(result.discover or {}),
                "profiled_companies": result.profiled_companies,
                "profile_observations": result.profile_observations,
                "errors": result.errors,
            }

        self._start(work)
        return {"queries": len(plan.queries)}

    async def submit_profile(self, limit: int = 200) -> dict:
        """Read the websites of everything discovered but not yet looked at.
        Free and model-free, so there is nothing to estimate and no cap."""
        self.ctx.require("run")
        if running():
            raise Conflict("a run is already going", code="job_running")
        from mercury.pipeline import run_profile_stage

        pending = await self.state.count_companies_needing_profile()
        if not pending:
            return {"pending": 0}

        async def work():
            companies, observations, _ = await run_profile_stage(self.state, limit=limit)
            return {"profiled_companies": companies, "profile_observations": observations}

        self._start(work)
        return {"pending": pending}

    async def stop(self, reason: str = "") -> dict:
        """Kill switch. Read between batches, so an in-flight run stops cleanly."""
        self.ctx.require("run")
        await self.state.set_setting("discovery_paused", reason or f"stopped from {self.ctx.client}")
        return self.job()
