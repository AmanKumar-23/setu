"""The numbers the dashboard puts in front of a human."""

from datetime import datetime, timedelta

import pytest


def test_work_queue_is_riskiest_then_oldest(srv, case, ago):
    queue = srv.work_queue([
        case(id="LOW_OLD",    escalation_risk="low",    opened_at=ago(hours=9)),
        case(id="HIGH_NEW",   escalation_risk="high",   opened_at=ago(minutes=5)),
        case(id="HIGH_OLD",   escalation_risk="high",   opened_at=ago(hours=3)),
        case(id="MED_OLD",    escalation_risk="medium", opened_at=ago(hours=6)),
    ])["queue"]
    assert [r["id"] for r in queue] == ["HIGH_OLD", "HIGH_NEW", "MED_OLD", "LOW_OLD"]


@pytest.mark.parametrize("status", ["resolved", "auto_resolved"])
def test_closed_cases_leave_the_queue(srv, case, status):
    assert srv.work_queue([case(status=status)])["total"] == 0


def test_deflection_rate(srv, case):
    stats = srv.suggestion_quality([])          # empty is safe
    assert stats["overall"]["rate"] is None

    quality = srv.suggestion_quality([
        {"ratings": [{"rating": "up", "grounded": True},
                     {"rating": "down", "grounded": True},
                     {"rating": "up", "grounded": True}]},
        {"ratings": [{"rating": "down", "grounded": False},
                     {"rating": None,   "grounded": False}]},   # produced, unrated
    ])
    assert quality["produced"] == 5
    assert quality["rated"] == 4
    assert quality["grounded"]["rate"] == 67       # 2 of 3
    assert quality["ungrounded"]["rate"] == 0      # 0 of 1


def test_performance_needs_four_cases_before_calling_a_trend(srv, case, ago):
    def scored(tone):
        return case(opened_at=ago(minutes=1), feedback={
            "tone_score": tone, "empathy_score": tone, "clarity_score": tone,
            "coaching_tip": "Open with a greeting"})

    assert srv.performance([scored(5)] * 3)["trend"] == {}

    improving = srv.performance([scored(2), scored(3), scored(8), scored(9)])
    assert improving["trend"]["tone"]["direction"] == "improving"

    steady = srv.performance([scored(6)] * 4)
    assert steady["trend"]["tone"]["direction"] == "steady"


def test_coaching_tips_group_into_themes(srv, case, ago):
    rows = [case(opened_at=ago(minutes=1), feedback={
        "tone_score": 5, "empathy_score": 5, "clarity_score": 5,
        "coaching_tip": "Add a greeting and give a specific timeline"})]
    labels = [t["label"] for t in srv.performance(rows)["themes"]]

    # One tip genuinely belongs to more than one theme.
    assert "Open with a greeting" in labels
    assert "Give a specific timeline" in labels


def test_the_timeline_fills_quiet_buckets(srv, case, ago):
    """Dropping quiet days would compress the time axis and make volume look
    steadier than it was."""
    days = srv.timeline([case(opened_at=ago(days=3), status="resolved"),
                         case(opened_at=ago(days=0), status="pending")])
    assert len(days) == 7                 # the default window, gaps and all
    assert sum(d["total"] for d in days) == 2


def test_the_timeline_survives_bad_timestamps(srv, case):
    assert srv.timeline([]) == []
    assert srv.timeline([case(opened_at="not-a-date")]) == []
    assert srv.timeline([{"status": "pending"}]) == []


@pytest.mark.parametrize("range_key,size", [
    ("24h", 24), ("7d", 7), ("14d", 14), ("30d", 30)])
def test_each_range_returns_its_own_window(srv, case, ago, range_key, size):
    points = srv.timeline([case(opened_at=ago(minutes=30))], range_key)
    assert len(points) == size


def test_twenty_four_hours_buckets_by_hour(srv, case, ago):
    """A day split into days is one or two bars, which is not a chart."""
    points = srv.timeline([case(opened_at=ago(minutes=30))], "24h")
    assert len({p["at"] for p in points}) == 24
    # consecutive buckets are an hour apart
    first, second = (datetime.fromisoformat(p["at"]) for p in points[:2])
    assert second - first == timedelta(hours=1)


def test_a_case_outside_the_window_is_not_counted(srv, case, ago):
    points = srv.timeline([case(opened_at=ago(days=20))], "7d")
    assert len(points) == 7               # the window still draws
    assert sum(p["total"] for p in points) == 0


def test_an_unknown_range_falls_back_to_the_default(srv, case, ago):
    points = srv.timeline([case(opened_at=ago(days=1))], "all-of-time")
    assert len(points) == 7


@pytest.mark.parametrize("kwargs,expected", [
    ({"status": "pending"},  ["A"]),
    ({"risk": "low"},        ["B"]),
    ({"sentiment": "negative"}, ["A"]),
    ({"query": "refund"},    ["A"]),
    ({"query": "zzz"},       []),
    ({},                     ["A", "B"]),
])
def test_filters(srv, case, kwargs, expected):
    cases = [
        case(id="A", status="pending", escalation_risk="high",
             sentiment="negative",
             messages=[{"speaker": "customer", "text": "where is my refund"}]),
        case(id="B", status="resolved", escalation_risk="low",
             sentiment="positive", messages=[]),
    ]
    assert [c["id"] for c in srv.filter_cases(cases, **kwargs)] == expected


def test_search_reaches_past_the_first_message(srv, case):
    """/api/cases strips the transcript, which is exactly why this filter runs
    on the server rather than in the browser."""
    cases = [case(id="A", messages=[
        {"speaker": "customer", "text": "hello"},
        {"speaker": "agent",    "text": "hi"},
        {"speaker": "customer", "text": "bahut ganda service hai"},
    ])]
    assert len(srv.filter_cases(cases, query="ganda")) == 1
