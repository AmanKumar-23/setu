"""How a message arrived -- dictated or typed.

Voice is an input method, not a pipeline. These check that the channel is
recorded and survives a save, and that recording it changes nothing else
about how the message is handled.
"""

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


def test_a_message_is_typed_unless_it_says_otherwise(srv):
    srv.session.state.add_message("customer", "my recharge failed")
    assert srv.session.state.history[0].channel == "typed"


def test_the_channel_is_recorded_on_the_message(srv):
    srv.session.state.add_message("customer", "spoken", channel="voice")
    assert srv.session.state.history[0].channel == "voice"


@pytest.mark.parametrize("given", ["shouted", "", None, "VOICE", 7])
def test_an_unrecognised_channel_is_treated_as_typed(srv, given):
    """The transcript must never claim a message was dictated on the strength
    of a value we do not understand."""
    assert srv.read_channel({"channel": given}) == "typed"


def test_the_channel_survives_a_save_and_a_reopen(srv):
    srv.session.state.add_message("customer", "spoken", channel="voice")
    srv.session.state.add_message("agent", "typed back")

    case = srv.session.as_case()
    assert [m["channel"] for m in case["messages"]] == ["voice", "typed"]

    srv.session.reset()
    srv.session.load(case)
    assert [m.channel for m in srv.session.state.history] == ["voice", "typed"]


def test_an_older_case_without_channels_reads_as_typed(srv, case):
    srv.session.load(case(messages=[{"speaker": "customer", "text": "old"}]))
    assert srv.session.state.history[0].channel == "typed"


def test_the_live_transcript_carries_the_channel(srv):
    srv.session.state.add_message("customer", "spoken", channel="voice")
    history = srv.session.as_dict()["history"]
    assert history[0]["channel"] == "voice"
    assert history[0]["source"] == "human"


def test_the_channel_does_not_change_how_a_message_is_handled(srv, client, monkeypatch):
    """Same text, same pipeline, whichever way it was entered."""
    seen = []
    monkeypatch.setattr(srv, "over_budget", lambda: None)
    monkeypatch.setattr(srv.session.coach, "analyze_customer_message",
                        lambda text, history: seen.append(text) or {
                            "sentiment": "negative", "urgency": "high",
                            "escalation_risk": "high", "frustration": 70,
                            "trend": "rising", "key_issue": "recharge",
                            "intent": "Recharge failed", "intent_confidence": 80,
                            "emotion": "Angry"})
    monkeypatch.setattr(srv.session.coach, "gather_facts", lambda *a, **k: [])
    monkeypatch.setattr(srv.session.coach, "suggest_reply", lambda *a, **k: "draft")
    monkeypatch.setattr(srv, "try_auto_resolve", lambda text: None)

    client.post("/api/customer", json={"text": "mera recharge fail ho gaya",
                                       "channel": "voice"})
    client.post("/api/customer", json={"text": "mera recharge fail ho gaya",
                                       "channel": "typed"})

    assert seen == ["mera recharge fail ho gaya"] * 2
    assert [m.channel for m in srv.session.state.history] == ["voice", "typed"]
