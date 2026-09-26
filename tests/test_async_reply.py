"""The assistant answers off the request.

Raising a ticket used to wait for four model calls -- 7.15s measured, far
more on a bad day -- before the customer saw anything. Now the request saves
the message and answers at once, and the model work happens on a worker
thread. These run the REAL thread (AI_INLINE off), because the thing worth
testing is exactly what happens when the request and the worker overlap.
"""

import threading
import time

import pytest


@pytest.fixture
def slow(srv, monkeypatch):
    """A model that takes as long as the test says, and counts its calls."""
    srv.app.config["AI_INLINE"] = False
    gate = {"delay": 0.0, "seen": [], "release": threading.Event()}
    gate["release"].set()

    def analyse(self, text, history=None):
        gate["seen"].append(text)
        gate["release"].wait(5)
        time.sleep(gate["delay"])
        return {"sentiment": "negative", "urgency": "medium",
                "escalation_risk": "medium", "key_issue": text[:40],
                "frustration": 50, "trend": "flat", "emotion": "Frustrated",
                "intent": "Other", "intent_confidence": 0.5}

    cls = srv.coach_core.AICoach
    monkeypatch.setattr(cls, "analyze_customer_message", analyse)
    monkeypatch.setattr(cls, "suggest_reply",
                        lambda self, text, *a, **k: f"draft for: {text}")
    monkeypatch.setattr(cls, "gather_facts",
                        lambda self, text, history, allow_writes=False: [])
    monkeypatch.setattr(srv.coach_core, "find_kb_article", lambda text: None)
    return gate


@pytest.fixture
def client(srv, slow):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("priya", "customer-pw", "customer")
    client = srv.app.test_client()
    client.post("/api/login", json={"username": "priya", "password": "customer-pw"})
    return client


def wait_until_answered(srv, case_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if case_id not in srv.AI_BUSY:
            case = srv.find_case(case_id)
            if case and not case.get("ai_pending"):
                return case
        time.sleep(0.02)
    raise AssertionError(f"{case_id} was never answered")


def raise_ticket(client, text="my recharge failed"):
    return client.post("/api/portal/tickets", json={
        "subject": "Recharge", "category": "Recharge", "description": text})


def test_raising_a_ticket_does_not_wait_for_the_model(srv, client, slow):
    slow["delay"] = 1.5                      # a slow model
    began = time.perf_counter()
    body = raise_ticket(client).get_json()
    took = time.perf_counter() - began

    assert body["ok"] is True
    assert took < 1.0, f"the request waited {took:.2f}s"
    assert body["ticket"]["ai_pending"] is True       # the chat shows "typing"
    assert body["ticket"]["messages"][0]["text"] == "my recharge failed"

    wait_until_answered(srv, body["ticket"]["id"])


def test_the_reply_lands_on_the_case_afterwards(srv, client):
    ticket = raise_ticket(client).get_json()["ticket"]
    case = wait_until_answered(srv, ticket["id"])
    assert case["ai_pending"] is False
    assert case["key_issue"] == "my recharge failed"      # the analysis arrived
    assert case["suggestion"] == "draft for: my recharge failed"
    assert set(case["perf"]) >= {"analyse", "lookup", "decide"}   # timings kept


def test_a_message_sent_mid_reply_is_kept_and_answered(srv, client, slow):
    """The race this whole design exists for. The customer writes again while
    the assistant is still on the first message: the second message must not
    be overwritten by the first reply's save, and it must get answered."""
    slow["release"].clear()                   # hold the worker mid-analysis
    ticket = raise_ticket(client, "first message").get_json()["ticket"]
    case_id = ticket["id"]

    deadline = time.time() + 5
    while "first message" not in slow["seen"] and time.time() < deadline:
        time.sleep(0.01)

    second = client.post(f"/api/portal/cases/{case_id}/message",
                         json={"text": "second message"})
    assert second.status_code == 200
    slow["release"].set()                     # let the first reply finish

    case = wait_until_answered(srv, case_id)
    texts = [m["text"] for m in case["messages"] if m["speaker"] == "customer"]
    assert texts == ["first message", "second message"]    # nothing lost
    assert slow["seen"][-1] == "second message"            # the newest answered
    assert case["key_issue"] == "second message"


def test_only_one_worker_runs_per_case(srv, client, slow):
    slow["release"].clear()
    ticket = raise_ticket(client, "one").get_json()["ticket"]
    for text in ("two", "three"):
        client.post(f"/api/portal/cases/{ticket['id']}/message", json={"text": text})
    slow["release"].set()
    wait_until_answered(srv, ticket["id"])
    # "one" first, then the worker picks up the newest -- never three workers
    # answering three messages in parallel and racing each other's saves.
    assert slow["seen"][0] == "one"
    assert "three" in slow["seen"]
    assert len(slow["seen"]) <= 3


def test_a_crashed_worker_never_leaves_the_chat_typing(srv, client, monkeypatch):
    def explode(*a, **k):
        raise RuntimeError("something nobody planned for")
    monkeypatch.setattr(srv.pipeline, "respond_to_customer", explode)

    ticket = raise_ticket(client).get_json()["ticket"]
    case = wait_until_answered(srv, ticket["id"])
    assert case["ai_pending"] is False
    assert case["handler"] == "human"                   # a person takes it


def test_two_tickets_at_once_get_different_ids(srv, client):
    ids, errors = [], []

    def go():
        try:
            ids.append(raise_ticket(client).get_json()["ticket"]["id"])
        except Exception as error:               # pragma: no cover
            errors.append(error)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(set(ids)) == 6
    for case_id in ids:
        wait_until_answered(srv, case_id)
