"""A newer return date must win over the sender's earlier due scan."""

from datetime import datetime, timedelta

import pytest

from mercury.state import StateManager


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["operator", "new_reply"])
async def test_due_scan_cannot_resume_an_extended_pause(tmp_path, monkeypatch, change):
    state = StateManager(str(tmp_path / "test.db"))
    await state.init_db()
    now = datetime(2026, 10, 8, 12)
    await state.record_ooo_pause(
        "contact_a", message_id="first@example.com", message_at=now - timedelta(days=2),
        state="paused", resume_at=now - timedelta(minutes=1),
    )
    item_id = await state.add_outbox_item(
        prospect_id="contact_a", to_email="contact@example.com", campaign_id="campaign_a",
        step=2, subject="Example", body="Example body", status="approved", send_at=now.isoformat(),
    )
    original = state.resume_pause
    extended = now + timedelta(days=3)

    async def resume_after_change(prospect_id, **kwargs):
        if change == "operator":
            await state.override_pause(prospect_id, extended, now=now)
        else:
            await state.record_ooo_pause(
                prospect_id, message_id="new@example.com", message_at=now,
                state="paused", resume_at=extended,
            )
        return await original(prospect_id, **kwargs)

    monkeypatch.setattr(state, "resume_pause", resume_after_change)
    assert await state.resume_due_pauses(now) == []
    pause = await state.get_active_pause("contact_a")
    assert pause["resume_at"] == extended.isoformat()
    assert not await state.get_outbox(status="approved", exclude_paused=True)
    assert (await state.get_outbox_item(item_id))["send_at"] == now.isoformat()
