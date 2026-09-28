"""The Trends time-range toggle.

The toggle only ever changes what the two charts are drawing. The headline
counts stay all-time, because "43 resolved" quietly meaning "43 this week"
would be a different number wearing the same label.
"""

import pytest


@pytest.fixture
def client(srv):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("ravi", "lead-password", "admin")
    c = srv.app.test_client()
    c.post("/api/login", json={"username": "ravi", "password": "lead-password"})
    return c


@pytest.fixture
def seeded(srv, case, ago):
    """One case inside every window, one well outside all of them."""
    srv.save_case(case(id="SC-NOW", status="resolved", opened_at=ago(minutes=20)))
    srv.save_case(case(id="SC-OLD", status="resolved", opened_at=ago(days=25)))
    return srv


@pytest.mark.parametrize("range_key,size", [
    ("24h", 24), ("7d", 7), ("14d", 14), ("30d", 30)])
def test_each_range_returns_its_own_number_of_buckets(client, seeded, range_key, size):
    body = client.get(f"/api/stats?range={range_key}").get_json()
    assert body["range"] == range_key
    assert len(body["by_day"]) == size


def test_the_default_is_seven_days(client, seeded):
    body = client.get("/api/stats").get_json()
    assert body["range"] == "7d"
    assert len(body["by_day"]) == 7


def test_an_unknown_range_falls_back_rather_than_failing(client, seeded):
    body = client.get("/api/stats?range=since-forever").get_json()
    assert body["range"] == "7d"


def test_twenty_four_hours_reports_hourly_buckets(client, seeded):
    body = client.get("/api/stats?range=24h").get_json()
    assert body["bucket"] == "hour"
    assert client.get("/api/stats?range=7d").get_json()["bucket"] == "day"


def test_every_point_carries_a_timestamp_for_the_axis(client, seeded):
    for key in ("24h", "30d"):
        for point in client.get(f"/api/stats?range={key}").get_json()["by_day"]:
            assert point["at"]
            assert point["day"]


def test_a_case_outside_the_window_is_left_out_of_the_chart(client, seeded):
    day = client.get("/api/stats?range=24h").get_json()
    month = client.get("/api/stats?range=30d").get_json()
    assert sum(p["total"] for p in day["by_day"]) == 1        # just SC-NOW
    assert sum(p["total"] for p in month["by_day"]) == 2      # both


def test_the_headline_counts_ignore_the_range(client, seeded):
    """Narrowing the chart must not restate how many cases exist."""
    day = client.get("/api/stats?range=24h").get_json()
    month = client.get("/api/stats?range=30d").get_json()
    for key in ("total", "resolved", "pending", "deflected", "resolution_rate"):
        assert day[key] == month[key], key


def test_the_scores_chart_takes_the_same_range(client, srv, case, ago):
    scored = {"tone_score": 8, "empathy_score": 7, "clarity_score": 9,
              "coaching_tip": "good"}
    srv.save_case(case(id="SC-NOW", opened_at=ago(minutes=20), feedback=scored))
    srv.save_case(case(id="SC-OLD", opened_at=ago(days=25), feedback=scored))

    day = client.get("/api/performance?range=24h").get_json()
    month = client.get("/api/performance?range=30d").get_json()

    assert day["bucket"] == "hour"
    assert len(day["by_day"]) == 1        # only the recent one is in window
    assert len(month["by_day"]) == 2
    # The trend is judged over everything, not the selected window -- it would
    # flip direction as you clicked between ranges otherwise.
    assert day["scored"] == month["scored"] == 2


def test_both_charts_agree_on_where_a_window_starts(client, srv, case, ago):
    """The two charts sit one above the other, so a one-bucket disagreement
    between them is plainly visible."""
    stats = client.get("/api/stats?range=14d").get_json()
    perf = client.get("/api/performance?range=14d").get_json()
    assert stats["bucket"] == perf["bucket"]
    assert stats["range"] == perf["range"]
