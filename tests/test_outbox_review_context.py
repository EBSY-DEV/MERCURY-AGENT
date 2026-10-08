"""What the Outbox review desk shows next to a draft: the contact, the rest of
its sequence, and the parts of the offer brief it was written from.

Synthetic fixtures only (offer_a / offer_c, example.com), shared with
tests/test_offers.py.
"""

from datetime import datetime, timedelta, timezone

from mercury.agents.sender import Sender
from mercury.agents.writer import Writer
from mercury.brain import Brain
from mercury.offers import OfferBrief, offer_by_key
from mercury.outbox_context import parse_brief
from tests.test_offers import client, fake_brain, make_config, run, seed  # noqa: F401  (client is a fixture)
from tests.test_outbox_native import Env, FakeProvider


def _brief(steps, facts=()):
    offer = offer_by_key(make_config(), "offer_a")
    return OfferBrief(offer=offer, steps=list(steps), reason="offer_a rule matched", facts=list(facts))


def test_parse_brief_reads_the_rendered_brief():
    prompt = "Write an email.\n" + _brief([1, 2, 3], ["- Search rank: 14 (observed 2026-10-04)"]).render() + "\n\nReturn JSON."
    first = parse_brief(prompt, 1)
    assert first["facts"] == ["Search rank: 14 (observed 2026-10-04)"]
    assert first["asks_for"] == "MARKER_A_CTA_1"
    # The offer's own restrictions; the line every brief ends with is not one.
    assert first["kept_out"] == ["MARKER_A_RESTRICTION"]
    assert parse_brief(prompt, 3)["asks_for"] == "MARKER_A_CTA_3"
    assert parse_brief("A prompt written before offers existed.", 1) is None


def _staged(client, monkeypatch):
    state = client.state
    ids = run(seed(state))
    fake_brain(monkeypatch)
    run(Writer(Brain(state), state, client.config).run())
    sender = Sender(None, state, client.config, Env())
    sender.provider = FakeProvider()
    for campaign in run(state.get_campaigns_by_status("draft")):
        run(sender._stage_campaign_native(campaign))
    return state, ids


def test_outbox_rows_carry_contact_sequence_and_brief(client, monkeypatch):
    state, ids = _staged(client, monkeypatch)
    pending = client.get("/api/outbox").json()["pending"]
    row = next(r for r in pending if r["prospect_id"] == ids["a"] and r["step"] == 1)

    assert row["contact"] == {
        "prospect_id": ids["a"], "name": "PatA Example", "first_name": "PatA", "title": "Owner",
        "company": "Example Bakery Delta", "company_id": row["contact"]["company_id"],
        "location": "Alphaville, Exampleland", "email_status": "verified"}
    assert row["contact"]["company_id"]

    seq = row["sequence"]
    assert seq["total"] == 3 and [s["step"] for s in seq["steps"]] == [1, 2, 3]
    assert seq["steps"][0]["id"] == row["id"]
    assert {s["status"] for s in seq["steps"]} == {"pending_review"}

    brief = row["brief"]
    assert brief["source"] == "generation"
    assert any("Search rank position: 14" in f for f in brief["facts"])
    assert brief["asks_for"] == "MARKER_A_CTA_1"
    assert brief["kept_out"] == ["MARKER_A_RESTRICTION"]

    # One email on its own reads the same.
    one = client.get(f"/api/outbox/{row['id']}").json()
    assert one["brief"] == brief and one["sequence"] == seq and one["contact"] == row["contact"]

    # A follow-up written from the shared template still names its own step's ask.
    step2 = next(r for r in pending if r["prospect_id"] == ids["a"] and r["step"] == 2)
    assert step2["brief"]["asks_for"] == "MARKER_A_CTA_2"


def test_brief_falls_back_to_the_configured_offer(client):
    state = client.state
    ids = run(seed(state))
    item = run(state.add_outbox_item(prospect_id=ids["a"], to_email="owner@a.example.com", subject="s",
                                     body="b", send_at="2026-10-08T12:00:00", step=2, campaign_id="camp_x",
                                     offer_key="offer_a"))
    row = client.get(f"/api/outbox/{item}").json()
    # No recorded generation: the ask and restrictions as configured, never invented facts.
    assert row["brief"] == {"facts": [], "asks_for": "MARKER_A_CTA_2",
                            "kept_out": ["MARKER_A_RESTRICTION"], "source": "config"}
    assert row["sequence"]["total"] == 2 and [s["id"] for s in row["sequence"]["steps"]] == [item]


def test_reply_has_no_sequence_or_brief(client):
    state = client.state
    ids = run(seed(state))
    item = run(state.add_outbox_item(prospect_id=ids["b"], to_email="owner@b.example.com", kind="reply",
                                     subject="Re: hello", body="Thanks", send_at="2026-10-08T12:00:00"))
    row = client.get(f"/api/outbox/{item}").json()
    assert row["sequence"] is None and row["brief"] is None
    assert row["contact"]["name"] == "PatB Example"


def test_sent_today_is_the_rolling_day(client):
    state = client.state
    ids = run(seed(state))
    now = datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)
    made = {}
    for tag, hours in (("recent", 2), ("old", 30)):
        item = run(state.add_outbox_item(prospect_id=ids["a"], to_email="owner@a.example.com",
                                         subject=tag, body="b", status="sent",
                                         send_at=(now - timedelta(hours=hours)).isoformat()))
        run(state.update_outbox_item(item, sent_at=(now - timedelta(hours=hours)).isoformat()))
        made[tag] = item
    data = client.get("/api/outbox").json()
    assert [r["id"] for r in data["sent_today"]] == [made["recent"]]
    assert data["sent_today"][0]["contact"]["name"] == "PatA Example"
    assert {r["id"] for r in data["sent"]} == set(made.values())
