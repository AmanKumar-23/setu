"""Customer satisfaction: the rules, the averages, and the honesty about them.

The averages here get shown as a single confident number on a tile, so what
these mostly defend is the sample size travelling with it.
"""

from datetime import UTC, datetime, timedelta

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


def closed(case, **kw):
    return case(status="resolved", **kw)


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------
def test_a_resolved_case_can_be_rated(srv, case):
    updated, why = srv.record_csat(closed(case), 5, "sorted in minutes")
    assert why is None
    assert updated["csat_score"] == 5
    assert updated["csat_comment"] == "sorted in minutes"
    assert updated["csat_at"]


def test_an_open_case_cannot_be_rated(srv, case):
    updated, why = srv.record_csat(case(status="pending"), 5)
    assert updated is None
    assert "not resolved" in why


def test_an_auto_resolved_case_can_be_rated(srv, case):
    """Deflection is still a resolution, and its rating is the one that shows
    whether deflecting actually worked."""
    updated, why = srv.record_csat(case(status="auto_resolved"), 4)
    assert why is None and updated["csat_score"] == 4


@pytest.mark.parametrize("score", [0, 6, -1, 99, "five", None, 2.5, ""])
def test_a_rating_outside_one_to_five_is_refused(srv, case, score):
    updated, why = srv.record_csat(closed(case), score)
    assert updated is None and why


def test_a_long_comment_is_trimmed_rather_than_refused(srv, case):
    updated, _ = srv.record_csat(closed(case), 3, "x" * 500)
    assert len(updated["csat_comment"]) == 280


def test_the_comment_is_optional(srv, case):
    updated, why = srv.record_csat(closed(case), 4)
    assert why is None and updated["csat_comment"] == ""


# --------------------------------------------------------------------------
# One per case, changeable for a day
# --------------------------------------------------------------------------
def test_a_rating_can_be_changed_inside_the_window(srv, case):
    rated, _ = srv.record_csat(closed(case), 2, "still waiting")
    first_at = rated["csat_at"]

    later = datetime.now(UTC) + timedelta(hours=23)
    changed, why = srv.record_csat(rated, 5, "they fixed it", now=later)

    assert why is None
    assert changed["csat_score"] == 5
    assert changed["csat_comment"] == "they fixed it"
    # The window anchors on the FIRST rating, or each edit would buy another
    # day and it would never close.
    assert changed["csat_at"] == first_at


def test_a_rating_cannot_be_changed_after_the_window(srv, case):
    rated, _ = srv.record_csat(closed(case), 2)
    later = datetime.now(UTC) + timedelta(hours=25)

    changed, why = srv.record_csat(rated, 5, now=later)
    assert changed is None
    assert "no longer be changed" in why
    assert rated["csat_score"] == 2          # untouched


def test_one_rating_per_case(srv, case):
    rated, _ = srv.record_csat(closed(case), 4)
    again, _ = srv.record_csat(rated, 1)
    assert again["csat_score"] == 1
    assert len([k for k in again if k.startswith("csat_")]) == 3


# --------------------------------------------------------------------------
# A rating must survive the console
# --------------------------------------------------------------------------
def test_an_agent_reopening_a_rated_case_does_not_wipe_the_rating(srv, case):
    """as_case() rebuilds the row from session state, which has never seen a
    rating. Without the carry-over in persist() this silently deleted it."""
    rated, _ = srv.record_csat(
        closed(case, id="SC-1", messages=[{"speaker": "customer", "text": "hi"}]), 5,
        "great")
    srv.save_case(rated)

    srv.session.load(srv.load_cases()[0])
    srv.session.state.add_message("agent", "one more thing")
    srv.session.persist()

    stored = srv.load_cases()[0]
    assert stored["csat_score"] == 5
    assert stored["csat_comment"] == "great"


# --------------------------------------------------------------------------
# The tile
# --------------------------------------------------------------------------
def test_the_average_is_two_decimals_over_rated_cases(srv, case):
    cases = [closed(case, id=f"SC-{i}", csat_score=s)
             for i, s in enumerate([5, 4, 4], 1)]
    summary = srv.csat_summary(cases)
    assert summary["average"] == 4.33
    assert summary["count"] == 3
    assert summary["eligible"] == 3
    assert summary["response_rate"] == 100


def test_the_response_rate_counts_every_closed_case(srv, case):
    cases = [closed(case, id="SC-1", csat_score=5),
             closed(case, id="SC-2"),
             closed(case, id="SC-3"),
             case(id="SC-4", status="pending")]        # not eligible
    summary = srv.csat_summary(cases)
    assert summary["eligible"] == 3
    assert summary["count"] == 1
    assert summary["response_rate"] == 33


def test_nothing_rated_reports_no_average_rather_than_zero(srv, case):
    summary = srv.csat_summary([closed(case, id="SC-1")])
    assert summary["average"] is None       # 0.00 out of 5 would be a claim
    assert summary["count"] == 0


def test_a_small_sample_is_flagged(srv, case):
    few = [closed(case, id=f"SC-{i}", csat_score=5) for i in range(9)]
    many = [closed(case, id=f"SC-{i}", csat_score=5) for i in range(10)]
    assert srv.csat_summary(few)["small_sample"] is True
    assert srv.csat_summary(many)["small_sample"] is False


def test_csat_by_mode_carries_its_own_sample_sizes(srv, case):
    cases = [
        closed(case, id="SC-1", csat_score=5, resolution_mode="ai_autonomous"),
        closed(case, id="SC-2", csat_score=3, resolution_mode="ai_autonomous"),
        closed(case, id="SC-3", csat_score=4, resolution_mode="human"),
    ]
    rows = {r["mode"]: r for r in srv.csat_by_mode(cases)}
    assert rows["ai_autonomous"]["average"] == 4.0
    assert rows["ai_autonomous"]["count"] == 2
    assert rows["hybrid"]["average"] is None
    assert rows["hybrid"]["count"] == 0
    assert all(r["small_sample"] for r in rows.values())


# --------------------------------------------------------------------------
# Median first reply
# --------------------------------------------------------------------------
def test_the_median_ignores_one_terrible_outlier(srv, case, ago):
    """The whole reason it is a median. The mean of these is over an hour."""
    def waited(cid, minutes):
        opened = ago(minutes=minutes + 5)
        return case(id=cid, opened_at=opened,
                    first_response_at=(datetime.fromisoformat(opened)
                                       + timedelta(minutes=minutes)
                                       ).isoformat(timespec="seconds"))

    cases = [waited("SC-1", 2), waited("SC-2", 3), waited("SC-3", 4),
             waited("SC-4", 5), waited("SC-5", 600)]
    result = srv.median_first_reply(cases)
    assert result["median_seconds"] == 4 * 60
    assert result["count"] == 5


def test_an_even_count_averages_the_middle_two(srv, case, ago):
    def waited(cid, minutes):
        opened = ago(minutes=minutes + 5)
        return case(id=cid, opened_at=opened,
                    first_response_at=(datetime.fromisoformat(opened)
                                       + timedelta(minutes=minutes)
                                       ).isoformat(timespec="seconds"))
    result = srv.median_first_reply([waited("SC-1", 2), waited("SC-2", 6)])
    assert result["median_seconds"] == 4 * 60


def test_cases_that_never_got_a_reply_are_left_out(srv, case):
    result = srv.median_first_reply([case(id="SC-1", first_response_at=None)])
    assert result["median_seconds"] is None and result["count"] == 0


# --------------------------------------------------------------------------
# Volume by category
# --------------------------------------------------------------------------
def test_categories_come_from_intent_and_are_sorted_by_count(srv, case):
    cases = ([case(id=f"A{i}", intent="Refund status") for i in range(3)]
             + [case(id=f"B{i}", intent="Network issue") for i in range(5)]
             + [case(id="C1", intent="Cancellation")])
    volume = srv.category_volume(cases)
    assert [r["category"] for r in volume["rows"]] == [
        "Network issue", "Refund status", "Cancellation"]
    assert volume["rows"][0]["count"] == 5
    assert volume["rows"][0]["share"] == 56          # 5 of 9
    assert volume["total"] == 9


def test_a_case_without_an_intent_falls_back_to_its_help_article(srv, case):
    """Older cases predate intents but recorded which article matched."""
    assert srv.category_of(case(auto_reply={"reply": "x", "topic": "refund"})) \
        == "Refund status"
    assert srv.category_of(case(ratings=[{"topic": "delivery"}])) \
        == "Order & delivery"


def test_a_case_with_neither_is_uncategorised_and_sorts_last(srv, case):
    cases = [case(id="A1"), case(id="A2"), case(id="B1", intent="Refund status")]
    rows = srv.category_volume(cases)["rows"]
    assert rows[-1]["category"] == "Uncategorised"
    assert rows[-1]["count"] == 2        # last despite being the biggest


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------
def test_rating_over_http(srv, client, case):
    srv.save_case(closed(case, id="SC-1", owner="ravi"))
    reply = client.post("/api/cases/SC-1/csat",
                        json={"score": 5, "comment": "quick"})
    assert reply.status_code == 200
    body = reply.get_json()
    assert body["csat_score"] == 5
    assert body["editable_for_hours"] == 24
    assert srv.load_cases()[0]["csat_score"] == 5


def test_rating_an_unknown_case_is_a_404(client):
    assert client.post("/api/cases/SC-NOPE/csat",
                       json={"score": 5}).status_code == 404


def test_a_bad_score_over_http_is_a_400(srv, client, case):
    srv.save_case(closed(case, id="SC-1", owner="ravi"))
    reply = client.post("/api/cases/SC-1/csat", json={"score": 9})
    assert reply.status_code == 400
    assert srv.load_cases()[0].get("csat_score") is None


def test_rating_needs_a_signed_in_caller(srv, client, case):
    srv.save_case(closed(case, id="SC-1", owner="ravi"))
    client.post("/api/logout")
    assert client.post("/api/cases/SC-1/csat",
                       json={"score": 5}).status_code == 401


def test_the_stats_endpoint_carries_every_new_number(srv, client, case):
    srv.save_case(closed(case, id="SC-1", csat_score=5, intent="Refund status"))
    body = client.get("/api/stats").get_json()
    assert body["csat"]["average"] == 5.0
    assert body["csat"]["small_sample"] is True
    assert len(body["csat_by_mode"]) == 3
    assert "median_seconds" in body["first_reply"]
    assert body["categories"]["rows"][0]["category"] == "Refund status"
