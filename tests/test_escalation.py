"""Escalation to a person, and the Escalated Queue.

Every customer message is scored on the four-level severity scale. High and
critical hand the case to a person: the customer is told, once, in their own
language, without a word about queues or scores; the case joins the
Escalated Queue, ordered by severity and then by how long it has waited; and
the agent can see the score and the rule that put it there.
"""

from datetime import UTC, datetime, timedelta

import pipeline
import pytest

HINDI = "मेरा रिफंड अभी तक नहीं आया, बहुत परेशानी हो रही है"
MACHINERY = ("escalat", "severity", "priority", "queue", "score", "flagged")


def reading(**over):
    base = {"sentiment": "negative", "urgency": "medium",
            "escalation_risk": "medium", "key_issue": "refund not received",
            "frustration": 45, "trend": "flat", "emotion": "Frustrated",
            "intent": "Refund status", "intent_confidence": 85}
    base.update(over)
    return base


ANGRY = dict(frustration=90, emotion="Angry", escalation_risk="high",
             intent="Complaint")


@pytest.fixture
def model(srv, monkeypatch):
    state = {"next": reading(), "handoffs": [], "replies": 0}

    def analyse(self, text, history=None):
        return dict(state["next"])

    def reply(self, *a, **k):
        state["replies"] += 1
        return f"reply #{state['replies']}"

    def handoff(self, text, history=None, key_issue="", language_note=""):
        state["handoffs"].append(language_note)
        return "I'm sorry about the wait. A member of our team is taking this over."

    cls = srv.coach_core.AICoach
    monkeypatch.setattr(cls, "analyze_customer_message", analyse)
    monkeypatch.setattr(cls, "suggest_reply", reply)
    monkeypatch.setattr(cls, "handoff_message", handoff)
    monkeypatch.setattr(cls, "gather_facts",
                        lambda self, text, history, allow_writes=False: [])
    monkeypatch.setattr(srv.coach_core, "find_kb_article", lambda text: None)
    return state


@pytest.fixture
def client(srv, model):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("priya", "customer-pw", "customer")
    auth.create_user("rahul", "agent-pwxx", "agent")
    client = srv.app.test_client()
    client.post("/api/login", json={"username": "priya", "password": "customer-pw"})
    return client


def raise_ticket(client, text="where is my refund"):
    return client.post("/api/portal/tickets", json={
        "subject": "Refund", "category": "Refund",
        "description": text}).get_json()["ticket"]


def stored(srv, case_id):
    return next(c for c in srv.load_cases() if c["id"] == case_id)


# ------------------------------------------------------------ 7a: escalation

@pytest.mark.parametrize("over,level", [
    (dict(frustration=70, escalation_risk="high"), "high"),
    (ANGRY, "critical"),
])
def test_high_and_critical_go_to_a_person(srv, client, model, over, level):
    model["next"] = reading(**over)
    case = stored(srv, raise_ticket(client)["id"])
    assert case["handler"] == "human"
    assert case["severity"] == level
    assert case["handover"]["severity"] == level
    assert case["handover"]["why"].startswith(f"severity {level}")
    # the assistant did not also answer them
    assert not [m for m in case["messages"] if m.get("source") == "ai"]
    # ...but the agent has a draft waiting
    assert case["suggestion"]


@pytest.mark.parametrize("over", [dict(frustration=10, sentiment="positive",
                                       emotion="Calm", escalation_risk="low"),
                                  dict(frustration=45)])
def test_low_and_medium_stay_with_the_assistant(srv, client, model, over):
    model["next"] = reading(**over)
    case = stored(srv, raise_ticket(client)["id"])
    assert case["handler"] == "ai"
    assert case.get("handover") is None


def test_the_customer_is_told_once_and_soothingly(srv, client, model):
    model["next"] = reading(**ANGRY)
    ticket = raise_ticket(client)
    client.post(f"/api/portal/cases/{ticket['id']}/message",
                json={"text": "still nothing, this is ridiculous"})

    seen = client.get(f"/api/portal/cases/{ticket['id']}").get_json()["ticket"]
    notes = [m for m in seen["messages"] if m["from"] == "Setu"]
    assert len(notes) == 1                       # once, at the moment of handover
    text = notes[0]["text"].lower()
    for word in MACHINERY:
        assert word not in text, word
    assert notes[0]["playable"] is True
    assert seen["status_label"] == "A human agent is reviewing"


def test_the_handoff_is_in_the_customers_language(srv, client, model):
    model["next"] = reading(**ANGRY)
    raise_ticket(client, HINDI)
    import languages
    assert model["handoffs"] == [languages.reply_instruction("hi")]


def test_if_the_model_cannot_write_it_the_customer_still_hears(srv, client,
                                                               model, monkeypatch):
    def broken(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(srv.coach_core.AICoach, "handoff_message", broken)
    model["next"] = reading(**ANGRY)
    case = stored(srv, raise_ticket(client, HINDI)["id"])
    note = next(m for m in case["messages"] if m.get("source") == "system")
    # the hand-written Hindi, never a model call while the model is down
    assert note["text"] == srv.ui_strings_cached("hi")["handoff.escalated"]
    for word in MACHINERY:
        assert word not in note["text"].lower()


def test_the_handoff_does_not_count_as_anyone_answering(srv, client, model):
    """It is a notice. The first person to reply stops the clock."""
    model["next"] = reading(**ANGRY)
    case = stored(srv, raise_ticket(client)["id"])
    assert case.get("first_response_at") is None
    assert srv.resolution_mode_for(case) is None


def test_the_customer_sees_none_of_the_reasoning(srv, client, model):
    model["next"] = reading(**ANGRY)
    ticket = raise_ticket(client)
    body = client.get(f"/api/portal/cases/{ticket['id']}").get_data(as_text=True)
    for leak in ("severity", "handover", "critical", "frustration", "Complaint"):
        assert leak not in body, leak


# --------------------------------------------------- 7b: the queue

def queue_as_agent(srv):
    agent = srv.app.test_client()
    agent.post("/api/login", json={"username": "rahul", "password": "agent-pwxx"})
    return agent.get("/api/queue?limit=50").get_json()


def test_the_queue_holds_escalated_cases_not_the_assistants(srv, client, model):
    model["next"] = reading()                                    # medium
    calm = raise_ticket(client)["id"]
    model["next"] = reading(**ANGRY)
    hot = raise_ticket(client)["id"]
    ids = [row["id"] for row in queue_as_agent(srv)["queue"]]
    assert hot in ids
    assert calm not in ids          # the assistant has it; nobody escalated it


def test_each_row_carries_what_the_brief_lists(srv, client, model):
    model["next"] = reading(**ANGRY)
    raise_ticket(client)
    row = queue_as_agent(srv)["queue"][0]
    for field in ("id", "severity", "subject", "first_message", "category",
                  "age_seconds", "severity_score", "severity_reasons"):
        assert field in row, field
    assert row["severity"] == "critical" and row["category"] == "Refund"


def test_severity_first_then_the_longest_wait(srv, case):
    now = datetime.now(UTC)

    def ago(hours):
        return (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    rows = [
        case(id="SC-A", opened_at=ago(1),  severity="critical", handler="human"),
        case(id="SC-B", opened_at=ago(30), severity="high",     handler="human"),
        case(id="SC-C", opened_at=ago(5),  severity="high",     handler="human"),
        case(id="SC-D", opened_at=ago(99), severity="medium",   handler="human"),
        case(id="SC-E", opened_at=ago(2),  severity="critical", handler="human"),
    ]
    order = [r["id"] for r in srv.work_queue(rows, limit=10)["queue"]]
    assert order == ["SC-E", "SC-A", "SC-B", "SC-C", "SC-D"]


def test_older_cases_keep_their_place_in_the_queue(srv, case):
    """Nothing already waiting on a person may vanish: cases from before the
    assistant have no handler, and their severity is read off the risk."""
    old = case(id="SC-OLD", escalation_risk="high", frustration=70)
    row = srv.work_queue([old])["queue"][0]
    assert row["id"] == "SC-OLD" and row["severity"] == "high"


# ------------------------------------------ opening a case from the queue

def test_opening_a_case_shows_its_severity_and_why(srv, client, model):
    model["next"] = reading(**ANGRY)
    ticket = raise_ticket(client)
    agent = srv.app.test_client()
    agent.post("/api/login", json={"username": "rahul", "password": "agent-pwxx"})
    state = agent.post("/api/open-case", json={"id": ticket["id"]}).get_json()["state"]
    assert state["severity"]["level"] == "critical"
    assert state["severity"]["reasons"]
    assert state["handover"]["why"].startswith("severity critical")
    assert state["sentiment"] == "negative" and state["emotion"] == "Angry"
    assert state["intent"] == "Complaint"
    assert state["last_suggestion"]                         # suggested reply
    assert [m for m in state["history"] if m["speaker"] == "customer"]


def test_a_case_never_analysed_is_analysed_when_opened(srv, client, model, case):
    """The model was down when it arrived. Opening it used to show "No
    signal" for ever; now it is read once, on open."""
    srv.save_case(case(id="SC-DARK", customer="priya", escalation_risk="unknown",
                       sentiment="unknown", handler="human",
                       messages=[{"speaker": "customer",
                                  "text": "my refund is late"}]))
    model["next"] = reading(frustration=70, escalation_risk="high")
    agent = srv.app.test_client()
    agent.post("/api/login", json={"username": "rahul", "password": "agent-pwxx"})
    state = agent.post("/api/open-case", json={"id": "SC-DARK"}).get_json()["state"]
    assert state["escalation_risk"] == "high"
    assert state["severity"]["level"] == "high"
    assert stored(srv, "SC-DARK")["escalation_risk"] == "high"   # kept


def test_severity_reasons_name_the_rule(srv):
    level, _score, reasons = pipeline.severity_of(
        reading(frustration=60), "I will go to the consumer court")
    assert level == "critical"
    assert any("consumer court" in r for r in reasons)


def test_opening_a_case_scores_the_draft_the_assistant_already_wrote(
        srv, client, model, monkeypatch):
    """The background assistant drafts a reply on every turn, so opening a
    case found a suggestion already there -- and returned before scoring it.
    The scorecard stayed empty until the agent had written something."""
    from coach_core import CoachingFeedback
    monkeypatch.setattr(srv.coach_core.AICoach, "evaluate_agent_response",
                        lambda self, c, a: CoachingFeedback(8, 7, 9, "Lead with the date."))
    model["next"] = reading(**ANGRY)
    ticket = raise_ticket(client)
    assert stored(srv, ticket["id"])["suggestion"]          # drafted already
    agent = srv.app.test_client()
    agent.post("/api/login", json={"username": "rahul", "password": "agent-pwxx"})
    state = agent.post("/api/open-case", json={"id": ticket["id"]}).get_json()["state"]
    assert state["last_feedback"]["tone_score"] == 8
    assert state["last_feedback"]["scored"] == "draft"      # not mistaken for a human's


def test_a_slow_turn_skips_the_optional_steps(srv, client, model, monkeypatch):
    """On a bad day each model call is bounded, but four of them in a row made
    a two-minute wait. Once the turn is slow, the lookup is skipped and the
    handoff uses the translated template instead of another model call."""
    clock = {"t": 0.0}
    monkeypatch.setattr(pipeline.time, "perf_counter", lambda: clock["t"])

    def slow_analyse(self, text, history=None):
        clock["t"] += 40                     # the analysis alone took 40s
        return reading(**ANGRY)

    calls = {"lookup": 0, "handoff": 0}

    def lookup(self, *a, **k):
        calls["lookup"] += 1
        return []

    def handoff(self, *a, **k):
        calls["handoff"] += 1
        return "bespoke"

    cls = srv.coach_core.AICoach
    monkeypatch.setattr(cls, "analyze_customer_message", slow_analyse)
    monkeypatch.setattr(cls, "gather_facts", lookup)
    monkeypatch.setattr(cls, "handoff_message", handoff)

    case = stored(srv, raise_ticket(client)["id"])
    assert calls == {"lookup": 0, "handoff": 0}
    note = next(m for m in case["messages"] if m.get("source") == "system")
    assert note["text"] == srv.UI_STRINGS["handoff.escalated"]   # the template
    assert case["handler"] == "human"                               # still escalated
    assert "lookup_skipped" in case["perf"]
