"""Only the reply travels. Analysis, lookups and scoring stay in English.

This is the load-bearing rule of the multilingual work, and the one that is
easiest to break by accident: someone adds `language_note` to one more prompt
and the dashboard quietly stops grouping. These tests pin the boundary at
both ends -- the language reaches the draft, and it reaches nothing else.
"""

import languages
import pipeline
import pytest


@pytest.fixture
def auth_mod(srv):
    import auth
    return auth


@pytest.fixture
def client(srv, auth_mod):
    auth_mod.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth_mod.create_user("priya", "customer-pw", "customer")
    auth_mod.create_user("rahul", "agent-pwxx", "agent")
    return srv.app.test_client()


def sign_in(client, who="priya", pw="customer-pw"):
    return client.post("/api/login", json={"username": who, "password": pw})


def stored_language(auth_mod, who):
    """What the users table holds, as the User class reads it: NULL is the
    column's untouched state and means English, not "no language"."""
    return auth_mod.find_user(who)["preferred_language"] or "en"


@pytest.fixture
def spy(srv, monkeypatch):
    """Record what each step of the pipeline was asked, and in what language."""
    seen = {"draft": [], "analyse": 0, "score": 0}

    def analyse(text, history=None):
        seen["analyse"] += 1
        return {"sentiment": "negative", "urgency": "high",
                "escalation_risk": "medium", "key_issue": "recharge failed",
                "frustration": 60, "trend": "rising", "emotion": "Frustrated",
                "intent": "Refund request", "intent_confidence": 0.8}

    def draft(text, history, analysis=None, facts=None, language_note=""):
        seen["draft"].append(language_note)
        return "Aapka refund 5 September tak aa jayega."

    # Patched on the CLASS, not on srv.session.coach: every portal request
    # builds its own short-lived session, so an instance patch would be
    # replaced by a live coach the moment a ticket is raised.
    cls = srv.coach_core.AICoach
    monkeypatch.setattr(cls, "analyze_customer_message",
                        lambda self, text, history=None: analyse(text, history))
    monkeypatch.setattr(cls, "suggest_reply",
                        lambda self, text, history, analysis=None, facts=None,
                        language_note="": draft(text, history, analysis, facts,
                                                language_note))
    # No key in CI, and a lookup is not what these tests are about.
    monkeypatch.setattr(cls, "gather_facts",
                        lambda self, text, history, allow_writes=False: [])
    monkeypatch.setattr(pipeline, "is_knowledge_gap", lambda text: False)
    monkeypatch.setattr(srv.coach_core, "find_kb_article", lambda text: None)
    return seen


# ---------------------------------------------------------------- the boundary

def test_the_chosen_language_reaches_the_draft(srv, client, spy, auth_mod):
    auth_mod.set_language("priya", "hi")
    sign_in(client)
    client.post("/api/portal/tickets",
                json={"subject": "Recharge", "category": "Recharge",
                      "description": "recharge failed, money gone"})
    assert spy["draft"], "the draft step never ran"
    assert spy["draft"][0] == languages.reply_instruction("hi")


def test_english_sends_no_instruction_at_all(srv, client, spy):
    """Spending tokens to tell the model to do what it already does by
    default is a cost with no effect."""
    sign_in(client)
    client.post("/api/portal/tickets",
                json={"subject": "Recharge", "category": "Recharge",
                      "description": "recharge failed"})
    assert spy["draft"][0] == ""


def test_analysis_is_stored_in_english_whatever_the_customer_wrote(
        srv, client, spy, auth_mod):
    """The Knowledge section groups on key_issue, the queue reads risk. Both
    would come apart if these fields followed the customer's language."""
    auth_mod.set_language("priya", "ta")
    sign_in(client)
    client.post("/api/portal/tickets",
                json={"subject": "Recharge", "category": "Recharge",
                      "description": "என் ரீசார்ஜ் "
                                     "தோல்வியடைந்தது"})
    stored = srv.load_cases()[0]
    assert stored["key_issue"] == "recharge failed"
    assert stored["sentiment"] == "negative"
    assert stored["escalation_risk"] == "medium"
    assert stored["language"] == "ta"          # the reply language, recorded


def test_switching_language_is_remembered_across_sessions(srv, client, auth_mod,
                                                          monkeypatch):
    # Setting a language now also builds the UI catalogue for it, which is a
    # model call for anything but English and Hindi. This test is about the
    # profile, not the words.
    monkeypatch.setattr(srv.coach_core.AICoach, "translate_lines",
                        lambda self, lines, note: list(lines))
    srv._UI_CACHE.clear()
    sign_in(client)
    assert client.post("/api/portal/language", json={"language": "bn"}).status_code == 200
    client.post("/api/logout")
    sign_in(client)
    d = client.get("/api/portal/languages").get_json()
    assert d["selected"] == "bn"
    assert stored_language(auth_mod, "priya") == "bn"


def test_an_unknown_language_is_refused_not_stored(client, auth_mod):
    sign_in(client)
    assert client.post("/api/portal/language", json={"language": "xx"}).status_code == 400
    assert stored_language(auth_mod, "priya") == "en"


# ------------------------------------------------------------- the offer

def test_writing_in_another_script_offers_a_switch_rather_than_taking_one(
        srv, client, spy, auth_mod):
    sign_in(client)
    d = client.post("/api/portal/tickets",
                    json={"subject": "Recharge", "category": "Recharge",
                          "description": "मेरा रिचार्ज "
                                         "फेल हो गया है"}).get_json()
    assert d["ticket"]["detected"] == "hi"
    assert d["ticket"]["detected_native"] == languages.native_name("hi")
    # Offered, not taken: the reply still went out in the chosen language.
    assert spy["draft"][0] == ""
    assert stored_language(auth_mod, "priya") == "en"


def test_no_offer_when_they_already_match(srv, client, spy, auth_mod):
    auth_mod.set_language("priya", "hi")
    sign_in(client)
    d = client.post("/api/portal/tickets",
                    json={"subject": "Recharge", "category": "Recharge",
                          "description": "मेरा रिचार्ज "
                                         "फेल हो गया है"}).get_json()
    assert d["ticket"]["detected"] is None


def test_the_customer_never_learns_what_detection_thought_about_them(
        srv, client, spy):
    """`detected` is the only new field on the customer's case. It says what
    language they wrote in -- nothing about how the message was judged."""
    sign_in(client)
    d = client.post("/api/portal/tickets",
                    json={"subject": "Recharge", "category": "Recharge",
                          "description": "recharge failed"}).get_json()
    for banned in ("sentiment", "urgency", "escalation_risk", "frustration",
                   "key_issue", "intent", "emotion", "suggestion"):
        assert banned not in d["ticket"]


# ------------------------------------------------------------- the agent gloss

def test_the_agent_gets_english_under_the_original_never_instead_of_it(
        srv, client, monkeypatch, case):
    srv.save_case(case(
        id="SC-77", customer="priya", owner="rahul", status="escalated",
        language="hi",
        messages=[{"speaker": "customer",
                   "text": "मेरा रिचार्ज "
                           "फेल हो गया है",
                   "source": "human", "channel": "typed"}]))
    monkeypatch.setattr(srv.session.coach, "translate_for_agent",
                        lambda text: "My recharge failed.")

    sign_in(client, "rahul", "agent-pwxx")
    d = client.post("/api/cases/SC-77/gloss").get_json()
    assert d["ok"]
    assert d["glosses"]["0"] == "My recharge failed."

    # The original is untouched on the case -- the gloss sits beside it.
    stored = next(c for c in srv.load_cases() if c["id"] == "SC-77")
    assert stored["messages"][0]["text"].startswith("मेरा")
    assert stored["messages"][0]["gloss_en"] == "My recharge failed."


def test_a_gloss_is_bought_once_however_often_the_case_is_opened(
        srv, client, monkeypatch, case):
    srv.save_case(case(
        id="SC-78", customer="priya", owner="rahul", status="escalated",
        messages=[{"speaker": "customer",
                   "text": "என் ரீசார்ஜ் "
                           "தோல்வி",
                   "source": "human", "channel": "typed"}]))
    calls = []
    monkeypatch.setattr(srv.session.coach, "translate_for_agent",
                        lambda text: calls.append(text) or "My recharge failed.")

    sign_in(client, "rahul", "agent-pwxx")
    client.post("/api/cases/SC-78/gloss")
    client.post("/api/cases/SC-78/gloss")
    assert len(calls) == 1


def test_an_english_conversation_buys_no_translation_at_all(
        srv, client, monkeypatch, case):
    srv.save_case(case(id="SC-79", customer="priya", owner="rahul",
                       status="escalated",
                       messages=[{"speaker": "customer",
                                  "text": "my recharge failed", "source": "human"}]))
    calls = []
    monkeypatch.setattr(srv.session.coach, "translate_for_agent",
                        lambda text: calls.append(text) or "x")

    sign_in(client, "rahul", "agent-pwxx")
    d = client.post("/api/cases/SC-79/gloss").get_json()
    assert d["glosses"] == {}
    assert calls == []


def test_a_customer_cannot_reach_the_gloss_route(client, case, srv):
    srv.save_case(case(id="SC-80", customer="priya"))
    sign_in(client)
    assert client.post("/api/cases/SC-80/gloss").status_code in (302, 403)


def test_the_gloss_survives_the_console_saving_the_case(srv, client, monkeypatch,
                                                        case, spy):
    """A translation is a paid call, so it rides on the message.

    The console keeps its own copy of the transcript and writes it back on
    every turn. If `gloss_en` did not travel through that save, the agent's
    next message would silently throw the translation away and the following
    open would buy it again.
    """
    hindi = ("मेरा रिचार्ज "
             "फेल हो गया है")
    srv.save_case(case(id="SC-81", customer="priya", owner="rahul",
                       status="escalated",
                       # Already drafted, so reopening it has no reason to ask
                       # the model for anything. Without this the test would
                       # pass or hang depending on how much budget earlier
                       # tests had spent, which is not what it is measuring.
                       suggestion="We are looking into it.",
                       messages=[{"speaker": "customer", "text": hindi,
                                  "source": "human", "channel": "typed"}]))
    calls = []
    # On the CLASS: open-case resets the live session, which builds a fresh
    # coach and would drop a patch made on the old instance.
    monkeypatch.setattr(srv.coach_core.AICoach, "translate_for_agent",
                        lambda self, text: calls.append(text) or "My recharge failed.")

    sign_in(client, "rahul", "agent-pwxx")
    client.post("/api/open-case", json={"id": "SC-81"})
    client.post("/api/cases/SC-81/gloss")

    # The console saves the case again, the way it does after every turn.
    with srv.app.test_request_context():
        srv.session.persist()

    stored = next(c for c in srv.load_cases() if c["id"] == "SC-81")
    assert stored["messages"][0]["gloss_en"] == "My recharge failed."

    client.post("/api/cases/SC-81/gloss")
    assert len(calls) == 1          # still bought exactly once
