"""Single-cycle mode -- the contract a scheduler depends on.

A scheduled run has no terminal to answer a setup wizard, no loop to retry
in, and nothing but an exit code to report with. It also has to stay honest
about quiet hours, because the schedule that wakes it knows nothing about
them. These tests pin all of that down, plus the root/sandbox handling that
decides whether Harvey's brain works at all in a hosted container.
"""

import os
from types import SimpleNamespace

import pytest

from harvey import brain as B
from harvey import main as M


def _config(percent=80, interval=15):
    return SimpleNamespace(
        usage=SimpleNamespace(
            max_daily_claude_percent=percent,
            heartbeat_interval_minutes=interval,
            quiet_hours=SimpleNamespace(start="22:00", end="07:00", timezone="UTC"),
        )
    )


class _Brain:
    def __init__(self, within_budget=True):
        self._within = within_budget

    async def is_within_budget(self, max_calls, max_percent=None):
        return self._within


class _State:
    def __init__(self, **summary):
        self.summary = summary
        self.logged = []

    async def get_state_summary(self):
        return self.summary

    async def log_action(self, action_type, agent):
        self.logged.append(action_type)


class _Agent:
    def __init__(self, name, ran, boom=False, is_native=False):
        self.name = name
        self.ran = ran
        self.boom = boom
        self.is_native = is_native

    async def run(self):
        self.ran.append(self.name)
        if self.boom:
            raise RuntimeError(f"{self.name} exploded")


def _runtime(state, brain=None, ran=None, native=False):
    ran = [] if ran is None else ran
    return M.Runtime(
        config=_config(),
        env=None,
        state=state,
        brain=brain or _Brain(),
        scout=_Agent("scout", ran),
        writer=_Agent("writer", ran),
        sender=_Agent("sender", ran, is_native=native),
        handler=_Agent("handler", ran),
        analyst=_Agent("analyst", ran),
    )


# --- run_cycle -------------------------------------------------------------

@pytest.mark.asyncio
async def test_over_budget_short_circuits_before_any_agent_runs():
    ran = []
    state = _State(prospects={"new": 0})
    rt = _runtime(state, brain=_Brain(within_budget=False), ran=ran)

    assert await M.run_cycle(rt) == "budget_exhausted"
    assert ran == []
    assert state.logged == []


@pytest.mark.asyncio
async def test_empty_pipeline_prospects_and_logs_the_action():
    ran = []
    state = _State(prospects={"new": 0}, draft_campaigns=0, open_conversations=0)
    rt = _runtime(state, ran=ran)

    assert await M.run_cycle(rt) == "prospect"
    assert "scout" in ran
    assert state.logged == ["prospect"]


@pytest.mark.asyncio
async def test_one_failing_agent_does_not_abort_the_cycle():
    """A scheduled run gets one shot; a single bad agent must not waste it."""
    ran = []
    state = _State(prospects={"new": 0}, draft_campaigns=0, open_conversations=0)
    rt = _runtime(state, ran=ran)
    rt.scout.boom = True

    assert await M.run_cycle(rt) == "prospect"
    assert state.logged == ["prospect"]


@pytest.mark.asyncio
async def test_native_sender_drains_the_outbox_alongside_other_work():
    ran = []
    state = _State(prospects={"new": 0}, draft_campaigns=0, open_conversations=0)
    rt = _runtime(state, ran=ran, native=True)

    await M.run_cycle(rt)
    assert "sender" in ran


# --- run_once --------------------------------------------------------------

@pytest.mark.asyncio
async def test_unusable_config_is_a_nonzero_exit(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(M, "build_runtime", _none)
    assert await M.run_once() == 1


@pytest.mark.asyncio
async def test_quiet_hours_skip_the_cycle_without_failing(monkeypatch):
    ran = []
    rt = _runtime(_State(prospects={"new": 0}), ran=ran)

    async def _rt():
        return rt

    monkeypatch.setattr(M, "build_runtime", _rt)
    monkeypatch.setattr(M, "in_quiet_hours", lambda config: True)

    assert await M.run_once() == 0
    assert ran == []


@pytest.mark.asyncio
async def test_ignore_quiet_hours_runs_anyway(monkeypatch):
    ran = []
    rt = _runtime(
        _State(prospects={"new": 0}, draft_campaigns=0, open_conversations=0), ran=ran
    )

    async def _rt():
        return rt

    monkeypatch.setattr(M, "build_runtime", _rt)
    monkeypatch.setattr(M, "in_quiet_hours", lambda config: True)

    assert await M.run_once(ignore_quiet_hours=True) == 0
    assert "scout" in ran


@pytest.mark.asyncio
async def test_a_crashed_cycle_reports_a_nonzero_exit(monkeypatch):
    rt = _runtime(_State(prospects={"new": 0}))

    async def _rt():
        return rt

    async def _boom(_rt_arg):
        raise RuntimeError("database is on fire")

    monkeypatch.setattr(M, "build_runtime", _rt)
    monkeypatch.setattr(M, "in_quiet_hours", lambda config: False)
    monkeypatch.setattr(M, "run_cycle", _boom)

    assert await M.run_once() == 1


def test_unconfigured_run_once_exits_nonzero_instead_of_prompting(monkeypatch):
    """The wizard would block forever in a container with no stdin."""
    monkeypatch.setattr(M, "_needs_setup", lambda: True)

    def _never(*a, **k):  # pragma: no cover - the point is it is not called
        raise AssertionError("run_once must not start the interactive wizard")

    monkeypatch.setattr(M, "run_once", _never)
    assert M.run_once_main() == 1


# --- credentials without a .env file ---------------------------------------

def test_environment_variables_count_as_configured(monkeypatch):
    """A container gets its keys injected, not written to a file."""
    monkeypatch.setattr(
        M, "load_env", lambda: SimpleNamespace(model_dump=lambda: {"hunter_api_key": "k"})
    )
    assert M._has_credentials() is True


def test_default_ports_alone_do_not_count_as_configured(monkeypatch):
    monkeypatch.setattr(
        M,
        "load_env",
        lambda: SimpleNamespace(
            model_dump=lambda: {"smtp_port": 587, "imap_port": 993, "hunter_api_key": ""}
        ),
    )
    assert M._has_credentials() is False


# --- brain subprocess environment ------------------------------------------

def test_root_container_gets_is_sandbox_so_the_cli_will_run(monkeypatch):
    """Without this the CLI refuses --dangerously-skip-permissions as root."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    assert B._cli_env()["IS_SANDBOX"] == "1"


def test_unprivileged_user_is_left_alone(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    assert "IS_SANDBOX" not in B._cli_env()


@pytest.mark.parametrize("inherited", ["yes", "true", "0", ""])
def test_an_unusable_inherited_value_is_overwritten(monkeypatch, inherited):
    """Hosts export IS_SANDBOX=yes; the CLI only accepts "1".

    Inheriting the host's spelling looks right and fails every Claude call,
    which is exactly how this was found.
    """
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setenv("IS_SANDBOX", inherited)
    assert B._cli_env()["IS_SANDBOX"] == "1"


# --- the offer actually reaching the reply agent -----------------------------

def _handler_with_offer(**offer_kwargs):
    from types import SimpleNamespace
    from harvey.agents.handler import Handler

    h = Handler.__new__(Handler)          # no I/O: only _offer_brief is under test
    defaults = dict(
        primary="", entry="", goal="", booking_method="",
        booking_url="", meeting_duration="", meeting_owner="",
    )
    defaults.update(offer_kwargs)
    h.config = SimpleNamespace(product=SimpleNamespace(offer=SimpleNamespace(**defaults)))
    return h


def test_a_calendar_link_reaches_the_prompt():
    """offer_strategy.md tells the agent to use booking_url; something must supply it."""
    brief = _handler_with_offer(
        goal="book_call",
        booking_method="calendar_link",
        booking_url="https://cal.com/someone/intro",
        meeting_owner="Someone Real",
    )._offer_brief()
    assert "https://cal.com/someone/intro" in brief
    assert "Someone Real" in brief


def test_suggest_times_never_promises_a_link():
    brief = _handler_with_offer(goal="book_call", booking_method="suggest_times")._offer_brief()
    assert "http" not in brief
    assert "do not send a link" in brief


def test_calendar_link_method_without_a_url_promises_nothing():
    """Half-configured is the dangerous case: don't tell it to send a link it lacks."""
    brief = _handler_with_offer(goal="book_call", booking_method="calendar_link")._offer_brief()
    assert "Booking link" not in brief


def test_an_unconfigured_offer_adds_no_section():
    assert _handler_with_offer()._offer_brief() == ""
