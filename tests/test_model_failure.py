"""When Gemini is slow, busy or simply not answering.

The report behind this file: raising a ticket showed the customer
"Gemini could not be reached after trying gemini-3.5-flash-lite,
gemini-3.5-flash. Last problem -> RemoteProtocolError: Server disconnected
without sending a response." Three separate things were wrong, and each
has a test here:

  1. a dropped connection was never retried -- only HTTP 503/429 were
  2. nothing bounded how long a call could take
  3. the exception text went straight to the customer
"""

import logging
import types as pytypes

import pytest


class RemoteProtocolError(Exception):
    """Stands in for httpx's. Only the NAME matters: that is what the retry
    list has to recognise, since the message is not an HTTP status."""


def make_coach(core, monkeypatch, outcomes):
    """An AICoach whose client plays back `outcomes` in order: an Exception
    instance is raised, anything else is returned as the response."""
    coach = core.AICoach.__new__(core.AICoach)
    coach.model = "gemini-test-lite"
    coach.last_redactions = []
    calls = []

    def generate_content(model, contents, config=None):
        calls.append(model)
        outcome = outcomes.pop(0) if outcomes else RuntimeError("out of script")
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    coach.client = pytypes.SimpleNamespace(
        models=pytypes.SimpleNamespace(generate_content=generate_content))
    monkeypatch.setattr(core.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(core, "report_usage", lambda *a, **k: None)
    return coach, calls


OK = pytypes.SimpleNamespace(text="ok", usage_metadata=None)


# ------------------------------------------------------------ retrying

def test_a_dropped_connection_is_retried(core, monkeypatch):
    coach, calls = make_coach(core, monkeypatch, [
        RemoteProtocolError("Server disconnected without sending a response."),
        OK,
    ])
    assert coach._call_model("hi") is OK
    assert calls == ["gemini-test-lite", "gemini-test-lite"]   # same model, again


def test_a_timeout_is_retried(core, monkeypatch):
    class ReadTimeout(Exception):
        pass
    coach, calls = make_coach(core, monkeypatch, [ReadTimeout("timed out"), OK])
    assert coach._call_model("hi") is OK
    assert len(calls) == 2


def test_a_rejected_request_is_not_retried(core, monkeypatch):
    """A 400 is our fault; every model rejects it the same way."""
    coach, calls = make_coach(core, monkeypatch, [
        RuntimeError("400 INVALID_ARGUMENT: bad schema")])
    with pytest.raises(core.ModelUnavailable):
        coach._call_model("hi")
    assert len(calls) == 1


def test_giving_up_raises_the_one_exception_type(core, monkeypatch):
    coach, _ = make_coach(core, monkeypatch, [
        RemoteProtocolError("Server disconnected") for _ in range(20)])
    with pytest.raises(core.ModelUnavailable) as caught:
        coach._call_model("hi")
    assert isinstance(caught.value, ValueError)      # old callers still work


def test_the_deadline_bounds_the_whole_call(core, monkeypatch):
    """However many models and attempts there are, the call stops at the
    ceiling instead of retrying for minutes."""
    clock = {"t": 0.0}
    monkeypatch.setattr(core.time, "monotonic", lambda: clock["t"])

    def slow_failure(*a, **k):
        clock["t"] += 20                       # every attempt burns 20s
        raise RemoteProtocolError("Server disconnected")

    coach, _ = make_coach(core, monkeypatch, [])
    coach.client.models.generate_content = slow_failure
    with pytest.raises(core.ModelUnavailable):
        coach._call_model("hi")
    assert clock["t"] <= core.AICoach.CALL_DEADLINE_S + 20   # at most one overrun


def test_every_failed_attempt_is_logged_for_the_dev_team(core, monkeypatch, caplog):
    coach, _ = make_coach(core, monkeypatch, [
        RemoteProtocolError("Server disconnected"), OK])
    with caplog.at_level(logging.WARNING, logger="setu.model"):
        coach._call_model("hi")
    assert any("RemoteProtocolError" in r.getMessage() for r in caplog.records)


def test_the_client_has_a_timeout(core):
    """Without one, a half-closed connection can hang a request forever."""
    assert core.AICoach.REQUEST_TIMEOUT_MS > 0
    assert core.AICoach.REQUEST_TIMEOUT_MS / 1000 < core.AICoach.CALL_DEADLINE_S


# ------------------------------------------- what the customer sees

RAW = ("RemoteProtocolError", "gemini", "Traceback", "Server disconnected",
       "could not be reached after trying")


@pytest.fixture
def client(srv, monkeypatch, core):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("priya", "customer-pw", "customer")

    def dead(self, *a, **k):
        raise core.ModelUnavailable(
            "Gemini could not be reached after trying gemini-3.5-flash-lite, "
            "gemini-3.5-flash. Last problem -> RemoteProtocolError: Server "
            "disconnected without sending a response.")

    cls = srv.coach_core.AICoach
    for name in ("analyze_customer_message", "suggest_reply", "gather_facts"):
        monkeypatch.setattr(cls, name, dead)
    monkeypatch.setattr(srv.coach_core, "find_kb_article", lambda text: None)
    client = srv.app.test_client()
    client.post("/api/login", json={"username": "priya", "password": "customer-pw"})
    return client


def raise_ticket(client):
    return client.post("/api/portal/tickets", json={
        "subject": "Recharge failed", "category": "Recharge",
        "description": "my recharge failed and money was deducted"})


def test_the_ticket_is_created_even_when_the_model_is_down(srv, client):
    reply = raise_ticket(client)
    assert reply.status_code == 200
    body = reply.get_json()
    assert body["ok"] is True
    # The request no longer waits on the model at all, so it cannot know the
    # model failed; the note in the thread is how the customer finds out.
    stored = next(c for c in srv.load_cases() if c["id"] == body["ticket"]["id"])
    assert stored["messages"][0]["text"] == "my recharge failed and money was deducted"


def test_the_customer_is_told_softly_and_a_person_takes_it(srv, client):
    body = raise_ticket(client).get_json()
    ticket = body["ticket"]
    note = ticket["messages"][-1]
    assert note["from"] == "Setu"
    assert "team" in note["text"].lower()
    assert ticket["status"] == "human_reviewing"
    stored = next(c for c in srv.load_cases() if c["id"] == ticket["id"])
    assert stored["handler"] == "human"


def test_no_raw_error_text_reaches_the_customer(client):
    text = raise_ticket(client).get_data(as_text=True)
    for leak in RAW:
        assert leak not in text, leak


def test_a_later_message_is_kept_too(srv, client):
    ticket = raise_ticket(client).get_json()["ticket"]
    reply = client.post(f"/api/portal/cases/{ticket['id']}/message",
                        json={"text": "any update?"})
    assert reply.status_code == 200
    text = reply.get_data(as_text=True)
    for leak in RAW:
        assert leak not in text, leak
    stored = next(c for c in srv.load_cases() if c["id"] == ticket["id"])
    assert any(m["text"] == "any update?" for m in stored["messages"])


def test_the_setu_note_does_not_count_as_anybody_handling_it(srv, client):
    """It is a notice. Counting it as a reply would put this case in the
    resolution split and stop the first-response clock on nothing."""
    ticket = raise_ticket(client).get_json()["ticket"]
    stored = next(c for c in srv.load_cases() if c["id"] == ticket["id"])
    assert srv.outgoing_sources(stored) == []
    assert srv.resolution_mode_for(stored) is None
    assert stored.get("first_response_at") is None


def test_the_error_is_logged_with_a_reference(srv, caplog):
    with srv.app.test_request_context("/api/portal/tickets", method="POST"):
        with caplog.at_level(logging.ERROR, logger="setu"):
            reply, status = srv.failure(RuntimeError("RemoteProtocolError: boom"))
    body = reply.get_json()
    assert status == 502
    assert "RemoteProtocolError" not in body["error"]
    assert body["ref"] and any(body["ref"] in r.getMessage() for r in caplog.records)
