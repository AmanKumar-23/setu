"""The queue in the agent console.

The console needed a queue an AGENT can reach -- /api/stats is lead-only --
but not a second idea of what to pick up next. What these mostly defend is
that /api/queue and the dashboard agree, because two rankings that drift
would have the two views recommending different cases.
"""

import pytest


@pytest.fixture
def auth_mod(srv):
    import auth
    return auth


@pytest.fixture
def client(srv, auth_mod):
    auth_mod.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth_mod.create_user("priya", "agent-password", "agent")
    auth_mod.create_user("ravi", "lead-password", "admin")
    return srv.app.test_client()


def sign_in(client, who, password):
    return client.post("/api/login", json={"username": who, "password": password})


@pytest.fixture
def backlog(srv, case, ago):
    """A spread of open cases, deliberately saved out of rank order."""
    srv.save_case(case(id="SC-LOW", escalation_risk="low", status="pending",
                       opened_at=ago(hours=9), owner="priya"))
    srv.save_case(case(id="SC-HIGH-NEW", escalation_risk="high", status="pending",
                       opened_at=ago(minutes=5), owner="priya"))
    srv.save_case(case(id="SC-HIGH-OLD", escalation_risk="high", status="pending",
                       opened_at=ago(hours=4), owner="priya"))
    srv.save_case(case(id="SC-MED", escalation_risk="medium", status="pending",
                       opened_at=ago(hours=2), owner="priya"))
    srv.save_case(case(id="SC-DONE", escalation_risk="high", status="resolved",
                       opened_at=ago(hours=1), owner="priya"))
    return srv


# --------------------------------------------------------------------------
# One ranking, two callers
# --------------------------------------------------------------------------
def test_the_console_queue_is_the_dashboard_queue(srv, client, backlog):
    """Byte-for-byte the same ordering function, not a re-implementation."""
    sign_in(client, "ravi", "lead-password")
    from_api = client.get("/api/queue").get_json()["queue"]
    direct = srv.work_queue(srv.load_cases(), limit=50)["queue"]
    assert [r["id"] for r in from_api] == [r["id"] for r in direct]


def test_riskiest_first_then_oldest(client, backlog):
    sign_in(client, "ravi", "lead-password")
    order = [r["id"] for r in client.get("/api/queue").get_json()["queue"]]
    assert order == ["SC-HIGH-OLD", "SC-HIGH-NEW", "SC-MED", "SC-LOW"]


def test_resolved_cases_are_not_in_the_queue(client, backlog):
    sign_in(client, "ravi", "lead-password")
    body = client.get("/api/queue").get_json()
    assert "SC-DONE" not in [r["id"] for r in body["queue"]]
    assert body["total"] == 4


# --------------------------------------------------------------------------
# Who can see it
# --------------------------------------------------------------------------
def test_a_signed_out_visitor_gets_nothing(client, backlog):
    assert client.get("/api/queue").status_code == 401


def test_an_agent_can_reach_it_even_though_stats_is_lead_only(client, backlog):
    sign_in(client, "priya", "agent-password")
    assert client.get("/api/stats").status_code == 403
    assert client.get("/api/queue").status_code == 200


def test_an_agent_only_sees_cases_they_can_open(srv, client, case, ago):
    """Ranking first and hiding afterwards would leave gaps in the list and a
    count that disagreed with it, so the filter happens before the sort."""
    srv.save_case(case(id="SC-MINE", status="pending", owner="priya",
                       escalation_risk="high", opened_at=ago(hours=2)))
    srv.save_case(case(id="SC-FREE", status="pending", owner=None,
                       escalation_risk="high", opened_at=ago(hours=1)))
    srv.save_case(case(id="SC-THEIRS", status="pending", owner="someone-else",
                       escalation_risk="high", opened_at=ago(hours=3)))

    sign_in(client, "priya", "agent-password")
    body = client.get("/api/queue").get_json()
    assert sorted(r["id"] for r in body["queue"]) == ["SC-FREE", "SC-MINE"]
    assert body["total"] == 2          # the count matches the list


def test_a_lead_sees_every_open_case(srv, client, case, ago):
    srv.save_case(case(id="SC-THEIRS", status="pending", owner="someone-else",
                       escalation_risk="high", opened_at=ago(hours=3)))
    sign_in(client, "ravi", "lead-password")
    assert [r["id"] for r in client.get("/api/queue").get_json()["queue"]] \
        == ["SC-THEIRS"]


# --------------------------------------------------------------------------
# What a card needs
# --------------------------------------------------------------------------
def test_every_card_carries_what_it_renders(client, backlog):
    sign_in(client, "ravi", "lead-password")
    row = client.get("/api/queue").get_json()["queue"][0]
    for field in ("id", "escalation_risk", "frustration", "age_seconds",
                  "first_message", "sla"):
        assert field in row, field
    assert "status" in row["sla"]          # the Critical filter reads this


def test_the_open_case_is_reported_so_it_can_be_shown_selected(srv, client, backlog):
    sign_in(client, "ravi", "lead-password")
    assert client.get("/api/queue").get_json()["open_case"] is None

    client.post("/api/open-case", json={"id": "SC-HIGH-OLD"})
    assert client.get("/api/queue").get_json()["open_case"] == "SC-HIGH-OLD"


@pytest.mark.parametrize("asked,expected", [("1", 1), ("500", 200), ("0", 1)])
def test_the_limit_is_clamped(client, backlog, asked, expected):
    sign_in(client, "ravi", "lead-password")
    body = client.get(f"/api/queue?limit={asked}").get_json()
    assert len(body["queue"]) <= expected
    # total always describes the backlog, not the page
    assert body["total"] == 4
