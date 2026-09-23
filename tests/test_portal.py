"""The customer's workspace, and the wall around it.

The rule these defend is narrow and absolute: a customer sees their own
tickets and three words of status, and nothing else. as_customer_case() is an
allow-list for exactly that reason -- a deny-list would leak every field
somebody adds to a case later.
"""

import typing

import pytest

# Everything the customer must never see, from the brief.
FORBIDDEN = ("sentiment", "urgency", "escalation_risk", "frustration", "trend",
             "feedback", "suggestion", "facts", "ratings", "redactions",
             "calls", "trajectory", "key_issue", "intent", "intent_confidence",
             "emotion", "resolution_mode", "unanswered", "auto_reply", "owner")


@pytest.fixture
def auth_mod(srv):
    import auth
    return auth


@pytest.fixture
def client(srv, auth_mod):
    auth_mod.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth_mod.create_user("priya", "customer-pw", "customer")
    auth_mod.create_user("dev", "customer-pw", "customer")      # another customer
    auth_mod.create_user("rahul", "agent-pwxx", "agent")
    auth_mod.create_user("root", "admin-pwxx", "admin")
    return srv.app.test_client()


def sign_in(client, who, pw):
    return client.post("/api/login", json={"username": who, "password": pw})


@pytest.fixture
def tickets(srv, case):
    srv.save_case(case(id="SC-MINE", customer="priya", status="auto_resolved",
                       subject="Refund timing", category="Refund",
                       messages=[{"speaker": "customer", "text": "how long?"},
                                 {"speaker": "agent", "text": "5-7 days",
                                  "source": "ai"}]))
    srv.save_case(case(id="SC-OPEN", customer="priya", status="pending",
                       subject="Recharge failed", category="Recharge"))
    srv.save_case(case(id="SC-THEIRS", customer="dev", status="pending",
                       subject="Someone else's", category="Other"))
    srv.save_case(case(id="SC-AGENT", customer=None, owner="rahul",
                       status="pending"))       # a console case, no customer
    return srv


# --------------------------------------------------------------------------
# Only your own
# --------------------------------------------------------------------------
def test_a_customer_sees_only_their_own_tickets(client, tickets):
    sign_in(client, "priya", "customer-pw")
    ids = [t["id"] for t in client.get("/api/portal/tickets").get_json()["tickets"]]
    assert sorted(ids) == ["SC-MINE", "SC-OPEN"]


def test_another_customers_ticket_is_a_404_not_a_403(client, tickets):
    """403 would confirm the case exists, which is itself a small leak."""
    sign_in(client, "priya", "customer-pw")
    assert client.get("/api/portal/cases/SC-THEIRS").status_code == 404
    assert client.post("/api/portal/cases/SC-THEIRS/message",
                       json={"text": "hello"}).status_code == 404


def test_the_chat_page_sends_you_home_with_a_message(client, tickets):
    sign_in(client, "priya", "customer-pw")
    res = client.get("/portal/chat/SC-THEIRS")
    assert res.status_code == 302
    assert "error=not-your-ticket" in res.headers["Location"]


def test_a_console_case_belongs_to_no_customer(client, tickets):
    sign_in(client, "priya", "customer-pw")
    assert client.get("/api/portal/cases/SC-AGENT").status_code == 404


# --------------------------------------------------------------------------
# The wall
# --------------------------------------------------------------------------
def test_nothing_internal_reaches_the_customer(srv, case):
    """The one test that matters. as_customer_case names what it emits, so a
    field added to a case later cannot ride along unnoticed."""
    rich = case(id="SC-1", customer="priya", status="pending",
                subject="S", category="Refund",
                sentiment="negative", urgency="high", escalation_risk="high",
                frustration=91, feedback={"tone_score": 3},
                suggestion="secret draft", facts=[{"name": "check_order_status"}],
                key_issue="internal note", intent="Refund status")

    shown = srv.as_customer_case(rich, with_messages=True)
    for field in FORBIDDEN:
        assert field not in shown, f"{field} leaked to the customer"


def test_staff_cannot_reach_the_portal_apis(client, tickets):
    for who, pw in [("rahul", "agent-pwxx"), ("root", "admin-pwxx")]:
        sign_in(client, who, pw)
        assert client.get("/api/portal/tickets").status_code == 403, who
        assert client.get("/api/portal/cases/SC-MINE").status_code == 403, who


# --------------------------------------------------------------------------
# Three states, one function
# --------------------------------------------------------------------------
@pytest.mark.parametrize("status,expected,tone", [
    ("auto_resolved", "ai_handled", "green"),
    ("resolved", "resolved", "grey"),
    ("pending", "human_reviewing", "amber"),
    ("escalated", "human_reviewing", "amber"),      # when the engine lands
    (None, "human_reviewing", "amber"),
])
def test_the_customer_sees_one_of_three_states(srv, case, status, expected, tone):
    c = case(status=status)
    assert srv.customer_status_of(c) == expected
    assert srv.as_customer_case(c)["status_tone"] == tone


def test_an_agents_reply_is_labelled_with_their_name(srv, case):
    """So a person taking over from the AI is visible to the customer."""
    c = case(customer="priya", messages=[
        {"speaker": "customer", "text": "hi", "source": "human"},
        {"speaker": "agent", "text": "AI here", "source": "ai"},
        {"speaker": "agent", "text": "Rahul here", "source": "human",
         "author": "Rahul Verma"},
    ])
    froms = [m["from"] for m in srv.as_customer_case(c, with_messages=True)["messages"]]
    assert froms == ["You", "AI assistant", "Rahul Verma"]


# --------------------------------------------------------------------------
# A new ticket is an ordinary case
# --------------------------------------------------------------------------
def test_a_raised_ticket_joins_the_same_case_log(client, srv, monkeypatch):
    """It must show up in the work queue and every dashboard count, because
    it is a row in the same table."""
    import pipeline
    monkeypatch.setattr(pipeline, "is_knowledge_gap", lambda text: False)
    monkeypatch.setattr(pipeline, "try_auto_resolve", lambda sess, text: None)
    monkeypatch.setattr(srv.LiveSession, "coach", property(lambda self: _FakeCoach()))

    sign_in(client, "priya", "customer-pw")
    reply = client.post("/api/portal/tickets", json={
        "subject": "Recharge failed", "category": "Recharge",
        "description": "mera recharge nahi hua but paise cut gaye"})

    assert reply.status_code == 200
    ticket = reply.get_json()["ticket"]

    stored = next(c for c in srv.load_cases() if c["id"] == ticket["id"])
    assert stored["customer"] == "priya"
    assert stored["subject"] == "Recharge failed"
    assert stored["category"] == "Recharge"
    # and it is in the queue the agents work from
    assert ticket["id"] in [r["id"] for r in srv.work_queue(srv.load_cases())["queue"]]


class _FakeCoach:
    model = "test"
    last_article = None
    last_redactions: typing.ClassVar[list] = []

    def analyze_customer_message(self, text, history):
        return {"sentiment": "negative", "urgency": "high",
                "escalation_risk": "high", "frustration": 70, "trend": "flat",
                "key_issue": "recharge", "intent": "Recharge failed",
                "intent_confidence": 80, "emotion": "Angry"}

    def gather_facts(self, *a, **k):
        return []

    def suggest_reply(self, *a, **k):
        return "draft for the agent"


# --------------------------------------------------------------------------
# Rating your own ticket
# --------------------------------------------------------------------------
def test_a_customer_can_rate_their_resolved_ticket(client, tickets, srv):
    sign_in(client, "priya", "customer-pw")
    reply = client.post("/api/portal/cases/SC-MINE/rating",
                        json={"score": 5, "comment": "sorted in minutes"})
    assert reply.status_code == 200

    ticket = reply.get_json()["ticket"]
    assert ticket["rating"] == 5
    assert ticket["rating_comment"] == "sorted in minutes"
    assert next(c for c in srv.load_cases() if c["id"] == "SC-MINE")["csat_score"] == 5


def test_an_open_ticket_cannot_be_rated_yet(client, tickets):
    """record_csat() refuses it, and can_rate tells the page not to ask."""
    sign_in(client, "priya", "customer-pw")
    assert client.post("/api/portal/cases/SC-OPEN/rating",
                       json={"score": 5}).status_code == 400
    ticket = client.get("/api/portal/cases/SC-OPEN").get_json()["ticket"]
    assert ticket["can_rate"] is False


def test_you_cannot_rate_somebody_elses_ticket(client, tickets):
    sign_in(client, "priya", "customer-pw")
    assert client.post("/api/portal/cases/SC-THEIRS/rating",
                       json={"score": 1}).status_code == 404


@pytest.mark.parametrize("score", [0, 6, -1, "five", None, 2.5])
def test_only_one_to_five_stars_are_accepted(client, tickets, score):
    sign_in(client, "priya", "customer-pw")
    assert client.post("/api/portal/cases/SC-MINE/rating",
                       json={"score": score}).status_code == 400


def test_a_rating_can_be_changed_inside_the_window(client, tickets):
    sign_in(client, "priya", "customer-pw")
    client.post("/api/portal/cases/SC-MINE/rating", json={"score": 2})
    again = client.post("/api/portal/cases/SC-MINE/rating",
                        json={"score": 5, "comment": "they fixed it"})
    assert again.get_json()["ticket"]["rating"] == 5


def test_the_portal_and_the_agent_side_share_one_set_of_rules(srv, case):
    """Both routes call record_csat(), so the stars, the comment limit and
    the 24-hour window cannot drift apart."""
    from datetime import UTC, datetime, timedelta
    rated, _ = srv.record_csat(case(status="resolved"), 4, "fine")
    assert rated["csat_score"] == 4
    stale, why = srv.record_csat(rated, 5,
                                 now=datetime.now(UTC) + timedelta(hours=25))
    assert stale is None and "no longer be changed" in why


def test_a_rating_reaches_the_dashboard_numbers(client, tickets, srv):
    """It is the same case log, so CSAT on the Overview moves with it."""
    sign_in(client, "priya", "customer-pw")
    client.post("/api/portal/cases/SC-MINE/rating", json={"score": 4})

    summary = srv.csat_summary(srv.load_cases())
    assert summary["count"] == 1
    assert summary["average"] == 4.0
