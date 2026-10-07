"""/api/heatmap: GitHub-style grid of outreach sends per day."""

from datetime import date, timedelta

from tests.test_trends import _at, _prospect, _run, _sent, _today, client  # noqa: F401
from mercury import metrics


def test_grid_is_monday_aligned_and_ends_today(client):
    data = client.get("/api/heatmap?weeks=53").json()
    start = date.fromisoformat(data["start"])
    assert start.weekday() == 0
    assert data["end"] == _today().isoformat()
    assert data["days"][-1]["date"] == data["end"]
    assert len(data["days"]) == (_today() - start).days + 1
    assert 52 * 7 < len(data["days"]) <= 53 * 7
    assert data["total_sent"] == 0 and data["best_day"] is None
    assert data["streak_current"] == 0 and data["streak_longest"] == 0


def test_counts_streaks_and_best_day(client):
    sm = client.sm
    # yesterday x2, two days ago x1, 10 days ago x3; nothing today
    for days_ago, n in ((1, 2), (2, 1), (10, 3)):
        for _ in range(n):
            _sent(sm, _prospect(sm), _at(days_ago))
    # a reply Mercury sent is not outreach
    _sent(sm, _prospect(sm), _at(1), kind="reply")

    data = client.get("/api/heatmap?weeks=4").json()
    by = {d["date"]: d["sent"] for d in data["days"]}
    assert by[(_today() - timedelta(days=1)).isoformat()] == 2
    assert by[(_today() - timedelta(days=10)).isoformat()] == 3
    assert data["total_sent"] == 6
    assert data["active_days"] == 3
    assert data["max"] == 3
    assert data["best_day"]["sent"] == 3
    # today is empty but the streak running to yesterday still counts
    assert data["streak_current"] == 2
    assert data["streak_longest"] == 2


def test_weeks_validation(client):
    assert client.get("/api/heatmap?weeks=0").status_code == 400
    assert client.get("/api/heatmap?weeks=54").status_code == 400
    assert client.get("/api/heatmap?weeks=abc").status_code == 400


def test_streak_breaks_on_gap(tmp_path):
    from mercury.state import StateManager
    db = str(tmp_path / "m.db")
    sm = StateManager(db_path=db)
    _run(sm.init_db())
    for days_ago in (0, 1, 3):
        _sent(sm, _prospect(sm), _at(days_ago))
    data = _run(metrics.heatmap(db, 2, _today()))
    assert data["streak_current"] == 2
    assert data["streak_longest"] == 2
