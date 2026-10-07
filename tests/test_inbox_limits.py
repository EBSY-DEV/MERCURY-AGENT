"""Inbox lifecycle limits: inboxes per domain, provider daily ceilings, the
14-day warm-up rule, and the age-keyed ramp that never more than doubles."""

from datetime import date, timedelta

import pytest

from mercury.config import EmailChannelConfig, MailboxConfig
from mercury.integrations.mailboxes import (
    Mailbox,
    MailboxPool,
    cap_source,
    full_volume_on,
    inbox_limit_warnings,
    mailbox_report,
    ramp_week_cap,
    warmup_cap,
)
from tests.test_mailboxes import make_config
from tests.test_outbox_native import FakeProvider

TODAY = date(2026, 10, 7)
OLD = TODAY - timedelta(days=90)


def cfg(mailboxes, **overrides):
    return make_config(mailboxes=mailboxes, **overrides)


def mb(email, cap=15, start=OLD, enabled=True):
    return MailboxConfig(email=email, daily_cap=cap, warmup_start=start, enabled=enabled)


def codes(warnings):
    return sorted(w["code"] for w in warnings)


# ── validation ──

def test_two_inboxes_on_a_domain_is_fine():
    assert inbox_limit_warnings(cfg([mb("a@x.co"), mb("b@x.co")]), TODAY) == []


def test_three_inboxes_on_one_domain_warns():
    w = inbox_limit_warnings(cfg([mb("a@x.co"), mb("b@x.co"), mb("c@x.co"), mb("d@y.co")]), TODAY)
    assert codes(w) == ["domain_inboxes"]
    assert w[0]["domain"] == "x.co"
    assert "3 inboxes" in w[0]["message"]


def test_domain_limit_is_configurable_and_can_be_switched_off():
    boxes = [mb("a@x.co"), mb("b@x.co"), mb("c@x.co")]
    assert inbox_limit_warnings(cfg(boxes, max_inboxes_per_domain=3), TODAY) == []
    assert inbox_limit_warnings(cfg(boxes, max_inboxes_per_domain=0), TODAY) == []


@pytest.mark.parametrize("provider,cap,expected", [
    ("smtp", 15, []), ("smtp", 16, ["cap_over_ceiling"]),
    ("gmail", 30, []), ("gmail", 31, ["cap_over_ceiling"]),
    ("instantly", 500, []),
])
def test_cap_over_provider_ceiling(provider, cap, expected):
    assert codes(inbox_limit_warnings(cfg([mb("a@x.co", cap=cap)], provider=provider), TODAY)) == expected


def test_ceiling_override():
    boxes = [mb("a@x.co", cap=25)]
    assert codes(inbox_limit_warnings(cfg(boxes), TODAY)) == ["cap_over_ceiling"]
    assert inbox_limit_warnings(cfg(boxes, provider_daily_ceilings={"smtp": 25}), TODAY) == []
    assert inbox_limit_warnings(cfg(boxes, provider_daily_ceilings={}), TODAY) == []


def test_young_inbox_with_cold_sends_enabled_warns():
    young = mb("a@x.co", start=TODAY - timedelta(days=5))
    assert codes(inbox_limit_warnings(cfg([young]), TODAY)) == ["young_inbox"]
    # 14 days old is old enough; a disabled inbox sends no cold mail; a
    # future start has not begun; no warmup_start means "already warm".
    assert inbox_limit_warnings(cfg([mb("a@x.co", start=TODAY - timedelta(days=14))]), TODAY) == []
    assert inbox_limit_warnings(cfg([mb("a@x.co", start=TODAY - timedelta(days=5), enabled=False)]), TODAY) == []
    assert inbox_limit_warnings(cfg([mb("a@x.co", start=TODAY + timedelta(days=3))]), TODAY) == []
    assert inbox_limit_warnings(cfg([mb("a@x.co", start=None)]), TODAY) == []


def test_legacy_configs_have_no_warnings_and_still_load():
    # No mailboxes list at all (single SMTP_* mailbox).
    assert inbox_limit_warnings(cfg([]), TODAY) == []
    # The new fields have defaults, so a pre-existing YAML dict loads as is.
    legacy = EmailChannelConfig(provider="smtp", mailboxes=[{"email": "a@x.co"}])
    assert legacy.max_inboxes_per_domain == 2
    assert legacy.provider_daily_ceilings == {"gmail": 30, "smtp": 15}
    # Warnings never change a cap.
    assert legacy.mailboxes[0].daily_cap == 30


def test_negative_domain_limit_rejected():
    with pytest.raises(ValueError):
        EmailChannelConfig(max_inboxes_per_domain=-1)


def test_report_carries_the_warnings():
    config = cfg([mb("a@x.co"), mb("b@x.co"), mb("c@x.co")])
    pool = MailboxPool([Mailbox(email=m.email, provider=FakeProvider(), daily_cap=m.daily_cap,
                                warmup_start=m.warmup_start) for m in config.channels.email.mailboxes])
    report = mailbox_report(config, pool, {}, TODAY)
    assert [w["code"] for w in report["limit_warnings"]] == ["domain_inboxes"]


# ── the age-keyed ramp ──

def test_ramp_table_weeks_0_to_6():
    # initial 5, +5 a week, cap 30: 5 10 15 20 25 30 30
    assert [ramp_week_cap(w, 30, 5, 5) for w in range(7)] == [5, 10, 15, 20, 25, 30, 30]


def test_ramp_by_inbox_age_in_days():
    start = TODAY
    caps = [warmup_cap(30, start, start + timedelta(days=d), 5, 5) for d in (0, 6, 7, 13, 14, 35, 42, 100)]
    assert caps == [5, 5, 10, 10, 15, 30, 30, 30]
    assert warmup_cap(30, start, start - timedelta(days=1), 5, 5) == 0


def test_no_week_more_than_doubles_the_previous():
    # +20 a week from 5 would be 5, 25, 45...: held to 5, 10, 20, 40.
    caps = [ramp_week_cap(w, 100, 5, 20) for w in range(6)]
    assert caps[:4] == [5, 10, 20, 40]
    assert all(b <= 2 * a for a, b in zip(caps, caps[1:]))
    assert caps[4:] == [80, 100]


def test_ramp_starting_at_zero_can_begin():
    assert [ramp_week_cap(w, 30, 0, 5) for w in range(3)] == [0, 5, 10]


def test_full_volume_follows_the_clamped_ramp():
    start = TODAY
    assert full_volume_on(30, start, 5, 5) == start + timedelta(weeks=5)
    assert full_volume_on(100, start, 5, 20) == start + timedelta(weeks=5)  # 5 10 20 40 80 100
    assert full_volume_on(30, start, 5, 0) is None
    assert full_volume_on(30, None, 5, 5) is None


def test_cap_source_names_the_limit():
    start = TODAY - timedelta(days=9)
    box = Mailbox(email="a@x.co", provider=FakeProvider(), daily_cap=30, warmup_start=start)
    key, why = cap_source(box, TODAY, 5, 5)
    assert key == "ramp" and "week 2" in why and "10/day" in why
    assert cap_source(box, TODAY, 5, 5, "paused")[0] == "paused"
    assert cap_source(box, TODAY, 5, 5, "hold")[0] == "hold"
    big = Mailbox(email="a@x.co", provider=FakeProvider(), daily_cap=100, warmup_start=start)
    assert "double" in cap_source(big, TODAY, 5, 40)[1]
    future = Mailbox(email="a@x.co", provider=FakeProvider(), daily_cap=30, warmup_start=TODAY + timedelta(days=2))
    assert cap_source(future, TODAY, 5, 5)[0] == "scheduled"
    warm = Mailbox(email="a@x.co", provider=FakeProvider(), daily_cap=30, warmup_start=OLD)
    assert cap_source(warm, TODAY, 5, 5)[0] == "daily_cap"
    assert cap_source(Mailbox(email="a@x.co", provider=FakeProvider(), daily_cap=30), TODAY, 5, 5)[0] == "daily_cap"


def test_report_rows_show_cap_and_reason():
    config = cfg([mb("a@x.co", cap=30, start=TODAY - timedelta(days=9))])
    row_box = config.channels.email.mailboxes[0]
    pool = MailboxPool([Mailbox(email=row_box.email, provider=FakeProvider(), daily_cap=30,
                                warmup_start=row_box.warmup_start)])
    row = mailbox_report(config, pool, {}, TODAY)["mailboxes"][0]
    assert row["cap_today"] == 10
    assert row["cap_source"] == "ramp"
    assert "week 2" in row["cap_reason"]
