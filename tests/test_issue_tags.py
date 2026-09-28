"""The Common issues card on /portal.

Every tag is a shortcut into a real conversation: a click raises a ticket
already filled in with that issue and goes straight to its chat. These pin
the three promises behind that -- the list is wider than it was, the counts
are real, and a tag produces an actual ticket in the backend.
"""

import pytest


@pytest.fixture
def client(srv, monkeypatch):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("priya", "customer-pw", "customer")
    auth.create_user("rahul", "agent-pwxx", "agent")

    # No model in CI. What these tests care about is the ticket, not the reply.
    cls = srv.coach_core.AICoach
    monkeypatch.setattr(cls, "analyze_customer_message",
                        lambda self, text, history=None: {
                            "sentiment": "negative", "urgency": "medium",
                            "escalation_risk": "medium", "key_issue": "x",
                            "frustration": 50, "trend": "flat",
                            "emotion": "Frustrated", "intent": "Other",
                            "intent_confidence": 0.5})
    monkeypatch.setattr(cls, "suggest_reply",
                        lambda self, *a, **k: "We are looking into it.")
    monkeypatch.setattr(cls, "gather_facts",
                        lambda self, text, history, allow_writes=False: [])
    monkeypatch.setattr(srv.coach_core, "find_kb_article", lambda text: None)
    return srv.app.test_client()


def sign_in(client, who="priya", pw="customer-pw"):
    return client.post("/api/login", json={"username": who, "password": pw})


# ------------------------------------------------------------ the list

def test_the_card_offers_more_than_the_original_six(srv, client):
    sign_in(client)
    rows = client.get("/api/portal/faqs").get_json()["faqs"]
    assert len(rows) > 6
    ids = {r["id"] for r in rows}
    for wanted in ("payment", "wrong-item", "locked", "subscription",
                   "app-crash", "profile", "invoice", "bulk"):
        assert wanted in ids


def test_every_tag_lands_in_a_real_category(srv, client):
    sign_in(client)
    for row in client.get("/api/portal/faqs").get_json()["faqs"]:
        assert row["category"] in srv.CATEGORIES, row["id"]


def test_the_agent_console_keeps_its_six_chips(srv, client):
    """The console and the dashboard's Knowledge groups read /api/faqs. They
    were not asked to change, so they do not."""
    sign_in(client, "rahul", "agent-pwxx")
    rows = client.get("/api/faqs").get_json()["faqs"]
    assert [r["id"] for r in rows] == ["recharge", "refund", "delivery",
                                        "network", "account", "escalate"]


def test_counts_are_live_not_hardcoded(srv, client, case):
    sign_in(client)
    before = {r["id"]: r["count"] for r in
              client.get("/api/portal/faqs").get_json()["faqs"]}

    srv.save_case(case(id="SC-9001", customer="priya",
                       key_issue="needs a GST invoice",
                       messages=[{"speaker": "customer",
                                  "text": "please send the gst invoice"}]))

    after = {r["id"]: r["count"] for r in
             client.get("/api/portal/faqs").get_json()["faqs"]}
    assert after["invoice"] == before["invoice"] + 1


# --------------------------------------------------- a click is a ticket

def test_a_tag_raises_a_ticket_with_its_category_and_origin(srv, client):
    sign_in(client)
    d = client.post("/api/portal/tickets", json={
        "subject": "Wrong item delivered", "category": "Order & Delivery",
        "description": "Wrong item delivered", "tag": "wrong-item"}).get_json()
    assert d["ok"] is True
    stored = next(c for c in srv.load_cases() if c["id"] == d["ticket"]["id"])
    assert stored["customer"] == "priya"
    assert stored["category"] == "Order & Delivery"
    assert stored["origin"] == "tag:wrong-item"
    # the chat page can open it straight away
    assert client.get(f"/api/portal/cases/{stored['id']}").status_code == 200


def test_the_form_is_recorded_as_the_form(srv, client):
    sign_in(client)
    d = client.post("/api/portal/tickets", json={
        "subject": "Something else", "category": "Other",
        "description": "a thing happened"}).get_json()
    stored = next(c for c in srv.load_cases() if c["id"] == d["ticket"]["id"])
    assert stored["origin"] == "form"


def test_an_invented_tag_is_not_believed(srv, client):
    sign_in(client)
    d = client.post("/api/portal/tickets", json={
        "subject": "x", "category": "Other", "description": "y",
        "tag": "not-a-real-tag"}).get_json()
    stored = next(c for c in srv.load_cases() if c["id"] == d["ticket"]["id"])
    assert stored["origin"] == "form"
