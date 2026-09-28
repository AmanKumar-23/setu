"""Intent, confidence and emotion -- the three fields added to the analyse step.

The point of these is less that the happy path works and more that a bad
value from the model cannot reach the panel. The schema's enum makes a wrong
word unlikely, but the lite model we fall back to on a rate limit does not
always honour the schema, so every one of these fields is forced into shape
after the call rather than trusted.
"""

import json

import pytest


@pytest.fixture
def coach(core):
    """An AICoach with no client -- __init__ would demand an API key."""
    obj = object.__new__(core.AICoach)
    obj.last_redactions = []
    obj.last_article = None
    obj.model = "gemini-3.5-flash"
    obj.client = None
    return obj


@pytest.fixture
def analyse(coach, monkeypatch):
    """Run the analyse step against a canned model reply, counting the calls."""
    calls = []

    def run(**reply):
        body = {"sentiment": "negative", "urgency": "high",
                "escalation_risk": "high", "frustration": 74, "trend": "rising",
                "key_issue": "Recharge failed, money debited"}
        body.update(reply)
        # A key set to None means "the model omitted it entirely".
        body = {k: v for k, v in body.items() if v is not ...}

        def fake(prompt, schema=None, **kwargs):
            calls.append(schema)
            return json.dumps(body)

        monkeypatch.setattr(coach, "_ask_model", fake)
        return coach.analyze_customer_message("mera recharge fail ho gaya")

    run.calls = calls
    return run


# --------------------------------------------------------------------------
# The one that matters for the Cost page
# --------------------------------------------------------------------------
def test_three_new_fields_cost_no_extra_api_call(analyse):
    """Intent extraction rides along on the analyse call.

    A second request would double what the Cost page bills to the Analyse
    step, which is the whole reason these fields went into the existing
    schema instead of a follow-up prompt.
    """
    analyse()
    assert len(analyse.calls) == 1, (
        f"analyse made {len(analyse.calls)} model calls, expected exactly 1")


def test_the_new_fields_ride_on_the_analysis_schema(analyse, core):
    analyse()
    assert analyse.calls[0] is core.ANALYSIS_SCHEMA


# --------------------------------------------------------------------------
# The vocabularies
# --------------------------------------------------------------------------
def test_intents_are_exactly_the_agreed_list(core):
    assert core.INTENTS == [
        "Recharge failed", "Refund status", "Order & delivery", "Network issue",
        "Account & login", "Billing dispute", "Cancellation", "Product question",
        # Added for the chat panel brief, which names "complaint" outright and
        # needed somewhere for the App & Technical category's tickets to land.
        "Complaint", "Technical issue",
        "Other",
    ]


def test_emotions_are_exactly_the_agreed_list(core):
    assert core.EMOTIONS == [
        "Calm", "Confused", "Frustrated", "Angry", "Anxious", "Satisfied"]


def test_the_schema_constrains_both_vocabularies(core):
    props = core.ANALYSIS_SCHEMA["properties"]
    assert props["intent"]["enum"] == core.INTENTS
    assert props["emotion"]["enum"] == core.EMOTIONS
    assert props["intent_confidence"]["type"] == "integer"
    for field in ("intent", "intent_confidence", "emotion"):
        assert field in core.ANALYSIS_SCHEMA["required"]


# --------------------------------------------------------------------------
# Intent
# --------------------------------------------------------------------------
def test_a_known_intent_passes_straight_through(analyse):
    reading = analyse(intent="Refund status", intent_confidence=88)
    assert reading["intent"] == "Refund status"
    assert reading["intent_confidence"] == 88


def test_an_unknown_intent_becomes_other_and_is_logged(analyse, capsys):
    reading = analyse(intent="Customer wants a unicorn")
    assert reading["intent"] == "Other"
    # The raw value has to survive into the log, or the prompt can drift for
    # weeks without anyone noticing which category the model invented.
    assert "Customer wants a unicorn" in capsys.readouterr().out


def test_a_missing_intent_becomes_other_quietly(analyse, capsys):
    reading = analyse(intent=...)
    assert reading["intent"] == "Other"
    # Absent is not the same as wrong -- nothing to log.
    assert "not in INTENTS" not in capsys.readouterr().out


# --------------------------------------------------------------------------
# Emotion
# --------------------------------------------------------------------------
def test_a_known_emotion_passes_straight_through(analyse):
    assert analyse(emotion="Frustrated")["emotion"] == "Frustrated"


def test_an_unusable_emotion_becomes_nothing_not_calm(analyse):
    """A wrong emotion must not become a reassuring one.

    Defaulting to "Calm" would assert something the model never said, on a
    panel whose entire purpose is catching the customer nobody noticed was
    angry. Empty renders as a dash, which claims nothing.
    """
    assert analyse(emotion="Incandescent")["emotion"] == ""
    assert analyse(emotion=...)["emotion"] == ""


# --------------------------------------------------------------------------
# Confidence
# --------------------------------------------------------------------------
def test_a_missing_confidence_stays_none_so_the_label_can_hide(analyse):
    assert analyse(intent_confidence=...)["intent_confidence"] is None


@pytest.mark.parametrize("given", ["high", None, "", [], {}])
def test_an_unusable_confidence_stays_none(analyse, given):
    assert analyse(intent_confidence=given)["intent_confidence"] is None


@pytest.mark.parametrize("given,expected", [(150, 100), (-20, 0), (0, 0),
                                            (100, 100), ("88", 88)])
def test_confidence_is_clamped_to_0_100(analyse, given, expected):
    assert analyse(intent_confidence=given)["intent_confidence"] == expected


# --------------------------------------------------------------------------
# Nothing that already worked may change
# --------------------------------------------------------------------------
def test_the_existing_readings_are_untouched(analyse):
    reading = analyse(intent="Refund status", emotion="Angry")
    assert reading["sentiment"] == "negative"
    assert reading["urgency"] == "high"
    assert reading["escalation_risk"] == "high"
    assert reading["frustration"] == 74
    assert reading["trend"] == "rising"
    assert reading["key_issue"] == "Recharge failed, money debited"


# --------------------------------------------------------------------------
# Carried onto the case and back again
# --------------------------------------------------------------------------
def test_the_case_record_carries_all_three(srv):
    srv.session.state.add_message("customer", "my recharge failed")
    srv.session.state.intent = "Recharge failed"
    srv.session.state.intent_confidence = 91
    srv.session.state.emotion = "Angry"

    case = srv.session.as_case()
    assert case["intent"] == "Recharge failed"
    assert case["intent_confidence"] == 91
    assert case["emotion"] == "Angry"


def test_the_live_panel_payload_carries_all_three(srv):
    srv.session.state.intent = "Refund status"
    srv.session.state.intent_confidence = 60
    srv.session.state.emotion = "Anxious"

    state = srv.session.as_dict()
    assert state["intent"] == "Refund status"
    assert state["intent_confidence"] == 60
    assert state["emotion"] == "Anxious"


def test_reopening_a_case_restores_all_three(srv, case):
    srv.session.load(case(intent="Network issue", intent_confidence=45,
                          emotion="Confused"))
    assert srv.session.state.intent == "Network issue"
    assert srv.session.state.intent_confidence == 45
    assert srv.session.state.emotion == "Confused"


def test_a_case_saved_before_this_feature_reopens_without_stale_readings(srv, case):
    """Cases from before intent extraction have none of these fields."""
    srv.session.state.intent = "Cancellation"
    srv.session.state.intent_confidence = 99
    srv.session.state.emotion = "Angry"

    srv.session.load(case())          # no intent/emotion keys at all

    assert srv.session.state.intent == ""
    assert srv.session.state.intent_confidence is None
    assert srv.session.state.emotion == ""
