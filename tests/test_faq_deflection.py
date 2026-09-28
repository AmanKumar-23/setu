"""Common issues, answered before they become tickets.

The card on /portal is a search with six shortcuts. A confident hit answers
the customer outright and nothing is raised; a miss opens the ticket form
with the category already chosen, and is logged to the same signal that
feeds "Questions we cannot answer" -- because a question we could not answer
is exactly what it was.
"""

import languages
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


@pytest.fixture
def answers(srv, monkeypatch):
    """Make the knowledge base confident and the draft cheap.

    `hits` flips the search between a confident semantic match and a miss,
    which are the only two branches the route has.
    """
    state = {"hit": True, "drafted": []}

    def find(text):
        if not state["hit"]:
            return None
        return {"how": "semantic", "score": 0.91, "topic": "refund_timeline",
                "answer": "Refunds land in 3-5 working days."}

    def draft(text, history, analysis=None, facts=None, language_note=""):
        state["drafted"].append((text, language_note))
        return "Your refund RF-9012 of ₹499 lands by 5 September."

    monkeypatch.setattr(srv.coach_core, "find_kb_article", find)
    monkeypatch.setattr(srv.session.coach, "suggest_reply", draft)
    return state


# ------------------------------------------------------------------ the card

def test_the_card_is_the_knowledge_groups_not_a_new_list(srv, client):
    sign_in(client)
    rows = client.get("/api/portal/faqs").get_json()["faqs"]
    assert {r["id"] for r in rows} == {f["id"] for f in srv.FAQS}


def test_counts_are_real_and_the_busiest_issue_leads(srv, client, case):
    srv.save_case(case(id="SC-1", customer="priya", key_issue="recharge failed",
                       messages=[{"speaker": "customer", "text": "recharge failed"}]))
    srv.save_case(case(id="SC-2", customer="priya", key_issue="recharge again",
                       messages=[{"speaker": "customer", "text": "recharge"}]))
    sign_in(client)
    rows = client.get("/api/portal/faqs").get_json()["faqs"]
    assert rows[0]["id"] == "recharge"
    assert rows[0]["count"] == 2


def test_the_card_is_customers_only(client):
    sign_in(client, "rahul", "agent-pwxx")
    assert client.get("/api/portal/faqs").status_code in (302, 403)


# ------------------------------------------------------------- a confident hit

def test_a_confident_article_answers_without_a_ticket(srv, client, answers):
    sign_in(client)
    d = client.post("/api/portal/faq/refund").get_json()
    assert d["answered"] is True
    assert d["answer"]
    assert load_ids(srv) == []          # nothing raised yet


def test_the_answer_is_drafted_from_the_customers_wording(srv, client, answers):
    """The FAQ's `text` is the agent's opening line, written for the console
    chips. Searching the knowledge base with it would look up the answer to a
    question nobody asked."""
    sign_in(client)
    client.post("/api/portal/faq/refund")
    asked, _ = answers["drafted"][0]
    faq = next(f for f in srv.FAQS if f["id"] == "refund")
    assert asked == faq["question"]
    assert asked != faq["text"]


def test_the_answer_is_drafted_in_the_customers_language(srv, client, answers,
                                                         auth_mod):
    auth_mod.set_language("priya", "ta")
    sign_in(client)
    client.post("/api/portal/faq/refund")
    _, note = answers["drafted"][0]
    assert note == languages.reply_instruction("ta")
    assert "Tamil" in note


# ------------------------------------------------------------- "this solved it"

def test_solved_records_a_closed_case_so_it_lands_in_the_deflection_rate(
        srv, client, answers):
    sign_in(client)
    client.post("/api/portal/faq/refund")
    d = client.post("/api/portal/faq/refund/solved",
                    json={"answer": "Refunds land in 3-5 days."}).get_json()
    assert d["ok"]

    stored = srv.load_cases()
    assert len(stored) == 1
    deflected = stored[0]
    assert deflected["status"] == "auto_resolved"
    assert deflected["closed_at"]
    assert deflected["deflected_from_faq"] == "refund"
    assert deflected["customer"] == "priya"
    assert deflected["owner"] is None          # no human ever touched it


def test_the_deflected_case_keeps_the_english_subject(srv, client, answers,
                                                      auth_mod):
    """The reply is translated; the subject is not. The agent console and the
    Knowledge grouping have to keep reading one vocabulary."""
    auth_mod.set_language("priya", "hi")
    sign_in(client)
    client.post("/api/portal/faq/refund")
    client.post("/api/portal/faq/refund/solved", json={"answer": "x"})
    stored = srv.load_cases()[0]
    faq = next(f for f in srv.FAQS if f["id"] == "refund")
    assert stored["subject"] == faq["label"]
    assert stored["language"] == "hi"


def test_a_deflection_never_reveals_analysis_to_the_customer(srv, client, answers):
    sign_in(client)
    client.post("/api/portal/faq/refund")
    d = client.post("/api/portal/faq/refund/solved", json={"answer": "x"}).get_json()
    for banned in ("sentiment", "escalation_risk", "urgency", "key_issue",
                   "suggestion", "auto_reply", "owner"):
        assert banned not in d["ticket"]


# ------------------------------------------------------------------- a miss

def test_a_miss_raises_no_ticket_and_hands_back_the_category(srv, client, answers):
    answers["hit"] = False
    sign_in(client)
    d = client.post("/api/portal/faq/network").get_json()
    assert d["answered"] is False
    assert d["category"] == "Network"
    assert load_ids(srv) == []


def test_a_miss_is_logged_where_the_knowledge_gaps_are_read(srv, client, answers):
    answers["hit"] = False
    sign_in(client)
    client.post("/api/portal/faq/network")

    logged = srv.faq_misses()
    assert len(logged) == 1
    faq = next(f for f in srv.FAQS if f["id"] == "network")
    assert logged[0]["text"] == faq["question"]
    assert logged[0]["who"] == "priya"


def test_an_unknown_issue_is_a_404_not_a_crash(client, answers):
    sign_in(client)
    assert client.post("/api/portal/faq/nonsense").status_code == 404
    assert client.post("/api/portal/faq/nonsense/solved").status_code == 404


def load_ids(srv):
    return [c["id"] for c in srv.load_cases()]
