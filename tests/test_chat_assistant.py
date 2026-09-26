"""The assistant in the customer's chat.

Once a ticket is raised the assistant answers each message itself: every
customer message is read for severity, intent and emotion, that reading is
kept against the message, and the reply comes back in the language the
customer just used.
"""

import languages
import pipeline
import pytest

HINDI = "नमस्ते, मेरा ऑर्डर अभी तक डिलीवर नहीं हुआ है, क्या आप मेरी मदद कर सकते हैं?"


def reading(**over):
    base = {"sentiment": "negative", "urgency": "medium",
            "escalation_risk": "medium", "key_issue": "order not delivered",
            "frustration": 50, "trend": "flat", "emotion": "Frustrated",
            "intent": "Order & delivery", "intent_confidence": 85}
    base.update(over)
    return base


@pytest.fixture
def model(srv, monkeypatch):
    """A scripted model. `next` is what the analysis returns; replies are
    written as "<lang> reply" so a test can see which language was asked."""
    state = {"next": reading(), "replies": []}

    def analyse(self, text, history=None):
        return dict(state["next"])

    def reply(self, text, history, analysis=None, facts=None, language_note=""):
        state["replies"].append({"text": text, "note": language_note,
                                 "analysis": analysis})
        return f"reply #{len(state['replies'])}"

    cls = srv.coach_core.AICoach
    monkeypatch.setattr(cls, "analyze_customer_message", analyse)
    monkeypatch.setattr(cls, "suggest_reply", reply)
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
    client = srv.app.test_client()
    client.post("/api/login", json={"username": "priya", "password": "customer-pw"})
    return client


def raise_ticket(client, text):
    return client.post("/api/portal/tickets", json={
        "subject": "Order", "category": "Order & Delivery",
        "description": text}).get_json()["ticket"]


def stored(srv, case_id):
    return next(c for c in srv.load_cases() if c["id"] == case_id)


# ------------------------------------------------------ the reply itself

def test_the_assistant_answers_the_customer_directly(srv, client):
    ticket = raise_ticket(client, "my order has not arrived")
    case = stored(srv, ticket["id"])
    replies = [m for m in case["messages"] if m["speaker"] == "agent"]
    assert [m["text"] for m in replies] == ["reply #1"]
    assert replies[0]["source"] == "ai"
    assert case["handler"] == "ai"
    assert case["first_response_at"]                     # the clock stopped
    assert case["status"] == "pending"                   # a reply is not a resolution


def test_the_customer_sees_the_reply_and_it_can_be_played(srv, client):
    ticket = raise_ticket(client, "my order has not arrived")
    seen = client.get(f"/api/portal/cases/{ticket['id']}").get_json()["ticket"]
    last = seen["messages"][-1]
    assert last["from"] == "AI assistant"
    assert last["playable"] is True
    assert last["language"] == "en"
    assert seen["status_label"] == "Handled by AI"


def test_the_reply_is_in_the_language_the_customer_used(srv, client, model):
    ticket = raise_ticket(client, HINDI)
    assert model["replies"][0]["note"] == languages.reply_instruction("hi")
    reply = stored(srv, ticket["id"])["messages"][-1]
    assert reply["language"] == "hi"                     # so audio uses a Hindi voice


def test_the_model_is_told_the_emotion_intent_and_severity(client, model):
    raise_ticket(client, "my order has not arrived")
    told = model["replies"][0]["analysis"]
    assert told["emotion"] == "Frustrated"
    assert told["intent"] == "Order & delivery"
    assert told["intent_confidence"] == 85
    assert told["severity"] in pipeline.SEVERITY_LEVELS


# ------------------------------------------------ tagging every message

def test_every_customer_message_is_tagged_and_kept(srv, client, model):
    ticket = raise_ticket(client, "my order has not arrived")
    model["next"] = reading(frustration=90, emotion="Angry",
                            escalation_risk="high", intent="Complaint")
    client.post(f"/api/portal/cases/{ticket['id']}/message",
                json={"text": "this is useless, I want to complain"})

    customer = [m for m in stored(srv, ticket["id"])["messages"]
                if m["speaker"] == "customer"]
    first, second = customer[0]["analysis"], customer[1]["analysis"]
    assert first["severity"] == "medium" and first["emotion"] == "Frustrated"
    assert second["severity"] == "critical" and second["emotion"] == "Angry"
    # The calm first reading was NOT overwritten by the angry second one.
    assert first["intent"] == "Order & delivery"
    for tag in ("severity", "severity_score", "severity_reasons", "sentiment",
                "intent", "intent_confidence", "emotion", "language"):
        assert tag in second


def test_the_customer_never_sees_any_of_the_tags(srv, client):
    ticket = raise_ticket(client, "my order has not arrived")
    seen = client.get(f"/api/portal/cases/{ticket['id']}").get_data(as_text=True)
    for banned in ("severity", "frustration", "intent", "emotion", "Frustrated"):
        assert banned not in seen, banned


# ------------------------------------------------------- the severity scale

@pytest.mark.parametrize("over,text,expect", [
    ({"frustration": 10, "sentiment": "positive", "emotion": "Calm",
      "escalation_risk": "low"}, "thanks, all good", "low"),
    ({"frustration": 40, "emotion": "Confused", "escalation_risk": "low",
      "sentiment": "neutral"}, "where is it?", "medium"),
    ({"frustration": 70, "escalation_risk": "high"}, "still nothing", "high"),
    ({"frustration": 55, "intent": "Cancellation", "escalation_risk": "medium"},
     "cancel my account", "high"),
    ({"frustration": 60, "escalation_risk": "medium"},
     "I will take this to the consumer court", "critical"),
    ({"frustration": 92, "emotion": "Angry"}, "worst service ever", "critical"),
])
def test_the_four_levels(over, text, expect):
    level, _score, reasons = pipeline.severity_of(reading(**over), text)
    assert level == expect
    assert reasons                                   # always says why


def test_high_means_worse_not_happier():
    """The scale points one way across the app: a satisfied customer is
    LOW, never high, so "escalate high and critical" can never escalate a
    happy one."""
    happy = reading(sentiment="positive", emotion="Satisfied", frustration=5,
                    escalation_risk="low", urgency="low")
    assert pipeline.severity_of(happy, "thank you so much!")[0] == "low"


# ---------------------------------------------- when a person must answer

@pytest.mark.parametrize("intent", ["Cancellation", "Billing dispute"])
def test_money_and_accounts_go_to_a_person(srv, client, model, intent):
    """Customers are never given write tools, so the assistant could only
    promise a cancellation or a reversal. It must not."""
    model["next"] = reading(intent=intent, frustration=30)
    ticket = raise_ticket(client, "please cancel it")
    case = stored(srv, ticket["id"])
    assert case["handler"] == "human"
    assert not [m for m in case["messages"]
                if m["speaker"] == "agent" and m["source"] == "ai"]
    assert case["suggestion"] == "reply #1"          # a draft for the agent


def test_a_case_a_person_has_taken_is_not_answered_by_the_assistant(
        srv, client, model):
    model["next"] = reading(intent="Cancellation")
    ticket = raise_ticket(client, "cancel")           # handed to a person
    model["next"] = reading()                        # calm again
    client.post(f"/api/portal/cases/{ticket['id']}/message",
                json={"text": "ok thanks, waiting"})
    case = stored(srv, ticket["id"])
    assert case["handler"] == "human"                 # it does not take it back
    assert not [m for m in case["messages"]
                if m["speaker"] == "agent" and m["source"] == "ai"]


def test_an_older_case_is_left_to_the_people_working_it(srv, client, case):
    """Cases from before the assistant answered anyone have no handler, and
    somebody may be mid-conversation on one."""
    srv.save_case(case(id="SC-OLD", customer="priya", status="pending",
                       messages=[{"speaker": "customer", "text": "hello"}]))
    client.post("/api/portal/cases/SC-OLD/message", json={"text": "any update?"})
    old = stored(srv, "SC-OLD")
    assert not [m for m in old["messages"] if m["speaker"] == "agent"]
