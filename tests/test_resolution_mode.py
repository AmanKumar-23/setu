"""Who actually resolved each case.

The three modes are derived, never typed in, so what these tests defend is
that the derivation says the same thing in both places it runs: live, where
each outgoing message records who composed it, and in the backfill, where
that has to be inferred from cases saved before the field existed.

If those two ever disagree, the dashboard shows a step change on the day the
feature shipped and calls it a trend.
"""

import pytest

DRAFT = ("I'm sorry about the failed recharge. I can see 499 was debited on "
         "30 August and the refund RF-9012 is processing, expected by 5 Sep.")


def msg(speaker, text, source=None):
    m = {"speaker": speaker, "text": text}
    if source:
        m["source"] = source
    return m


# --------------------------------------------------------------------------
# The rules, on messages that carry their own provenance
# --------------------------------------------------------------------------
def test_only_ai_messages_is_autonomous(srv, case):
    c = case(messages=[msg("customer", "my recharge failed"),
                       msg("agent", DRAFT, "ai")])
    assert srv.resolution_mode_for(c) == "ai_autonomous"


def test_a_used_draft_is_hybrid(srv, case):
    c = case(messages=[msg("customer", "my recharge failed"),
                       msg("agent", DRAFT, "hybrid")])
    assert srv.resolution_mode_for(c) == "hybrid"


def test_all_hand_written_is_human(srv, case):
    c = case(messages=[msg("customer", "my recharge failed"),
                       msg("agent", "Give me two minutes.", "human"),
                       msg("agent", "Sorted, refund is on its way.", "human")])
    assert srv.resolution_mode_for(c) == "human"


def test_one_used_draft_among_many_is_enough(srv, case):
    c = case(messages=[msg("agent", "Two minutes.", "human"),
                       msg("agent", DRAFT, "hybrid"),
                       msg("agent", "Anything else?", "human")])
    assert srv.resolution_mode_for(c) == "hybrid"


def test_a_deflection_a_human_followed_up_is_hybrid(srv, case):
    """Neither label fits on its own, so it goes in the middle bucket.

    Calling it autonomous would overstate the number the whole feature exists
    to report; calling it human would claim nobody used the coach.
    """
    c = case(messages=[msg("agent", DRAFT, "ai"),
                       msg("agent", "Following up -- did that land?", "human")])
    assert srv.resolution_mode_for(c) == "hybrid"


def test_a_case_nobody_replied_to_has_no_mode(srv, case):
    c = case(messages=[msg("customer", "my recharge failed")])
    assert srv.resolution_mode_for(c) is None


def test_customer_messages_do_not_count_as_outgoing(srv, case):
    c = case(messages=[msg("customer", "a"), msg("customer", "b"),
                       msg("agent", DRAFT, "ai")])
    assert srv.outgoing_sources(c) == ["ai"]


# --------------------------------------------------------------------------
# Inference, for cases saved before the field existed
# --------------------------------------------------------------------------
def test_a_reply_matching_the_draft_is_inferred_hybrid(srv, case):
    c = case(suggestion=DRAFT,
             messages=[msg("customer", "my recharge failed"),
                       msg("agent", DRAFT)])          # no source recorded
    assert srv.resolution_mode_for(c) == "hybrid"


def test_a_lightly_edited_draft_is_still_hybrid(srv, case):
    edited = DRAFT.replace("I'm sorry about", "Apologies for") + " Thanks!"
    c = case(suggestion=DRAFT, messages=[msg("agent", edited)])
    assert srv.similarity(edited, DRAFT) > srv.HYBRID_SIMILARITY
    assert srv.resolution_mode_for(c) == "hybrid"


def test_a_reply_that_ignores_the_draft_is_human(srv, case):
    c = case(suggestion=DRAFT,
             messages=[msg("agent", "Call the operator, nothing we can do.")])
    assert srv.resolution_mode_for(c) == "human"


def test_the_stored_deflection_is_inferred_as_ai(srv, case):
    c = case(auto_reply={"reply": DRAFT, "topic": "refunds"},
             messages=[msg("customer", "when is my refund"),
                       msg("agent", DRAFT)])
    assert srv.resolution_mode_for(c) == "ai_autonomous"


def test_a_recorded_source_always_beats_inference(srv, case):
    """A message that says what it is must not be second-guessed by a string
    comparison -- the recorded value was set when somebody actually knew."""
    c = case(suggestion=DRAFT, messages=[msg("agent", DRAFT, "human")])
    assert srv.resolution_mode_for(c) == "human"


@pytest.mark.parametrize("ratio_text,expected", [
    (DRAFT, "hybrid"),                                  # identical
    ("Completely different words entirely here.", "human"),
])
def test_the_threshold_is_the_only_thing_deciding(srv, case, ratio_text, expected):
    c = case(suggestion=DRAFT, messages=[msg("agent", ratio_text)])
    assert srv.resolution_mode_for(c) == expected


# --------------------------------------------------------------------------
# The backfill
# --------------------------------------------------------------------------
def test_the_backfill_fills_older_cases(srv, case):
    srv.save_case(case(id="SC-1", suggestion=DRAFT,
                       messages=[msg("agent", DRAFT)]))
    srv.save_case(case(id="SC-2", messages=[msg("agent", "My own words.")]))
    srv.save_case(case(id="SC-3", auto_reply={"reply": DRAFT},
                       messages=[msg("agent", DRAFT)]))

    assert srv.backfill_resolution_modes() == 3

    modes = {c["id"]: c["resolution_mode"] for c in srv.load_cases()}
    assert modes == {"SC-1": "hybrid", "SC-2": "human", "SC-3": "ai_autonomous"}


def test_the_backfill_runs_once(srv, case):
    srv.save_case(case(id="SC-1", messages=[msg("agent", "mine")]))
    assert srv.backfill_resolution_modes() == 1
    assert srv.backfill_resolution_modes() == 0


def test_the_backfill_skips_a_case_with_nothing_sent(srv, case):
    srv.save_case(case(id="SC-1", messages=[msg("customer", "hello")]))
    assert srv.backfill_resolution_modes() == 0
    assert srv.load_cases()[0].get("resolution_mode") is None


# --------------------------------------------------------------------------
# Saving, filtering and the numbers on the dashboard
# --------------------------------------------------------------------------
def test_saving_a_case_derives_the_mode(srv):
    srv.session.state.add_message("customer", "my recharge failed")
    srv.session.state.add_message("agent", DRAFT, "hybrid")
    assert srv.session.as_case()["resolution_mode"] == "hybrid"


def test_filtering_by_mode(srv, case):
    cases = [
        case(id="SC-1", resolution_mode="ai_autonomous"),
        case(id="SC-2", resolution_mode="hybrid"),
        case(id="SC-3", resolution_mode="human"),
    ]
    kept = srv.filter_cases(cases, mode="hybrid")
    assert [c["id"] for c in kept] == ["SC-2"]
    assert len(srv.filter_cases(cases, mode="all")) == 3


def test_the_two_derived_numbers_are_exact_inverses(srv, case, client):
    for i, mode in enumerate(["ai_autonomous", "ai_autonomous", "hybrid"], 1):
        srv.save_case(case(id=f"SC-{i}", status="resolved",
                           resolution_mode=mode))

    stats = client.get("/api/stats").get_json()
    assert stats["by_mode"] == {"ai_autonomous": 2, "hybrid": 1, "human": 0}
    assert stats["modes_counted"] == 3
    assert stats["ai_end_to_end"] == 67
    assert stats["human_touch_rate"] == 33
    assert stats["ai_end_to_end"] + stats["human_touch_rate"] == 100


def test_open_cases_are_not_counted_as_resolved_by_anyone(srv, case, client):
    """A case still waiting on a reply has no resolver, and must not dilute
    the shares of the ones that are actually closed."""
    srv.save_case(case(id="SC-1", status="resolved",
                       resolution_mode="ai_autonomous"))
    srv.save_case(case(id="SC-2", status="pending",
                       messages=[msg("customer", "still waiting")]))

    stats = client.get("/api/stats").get_json()
    assert stats["modes_counted"] == 1
    assert stats["ai_end_to_end"] == 100


def test_the_daily_split_adds_up(srv, case, client):
    srv.save_case(case(id="SC-1", status="resolved", resolution_mode="hybrid"))
    srv.save_case(case(id="SC-2", status="resolved", resolution_mode="human"))

    days = client.get("/api/stats").get_json()["by_day"]
    totals = {k: sum(d[k] for d in days)
              for k in ("ai_autonomous", "hybrid", "human")}
    assert totals == {"ai_autonomous": 0, "hybrid": 1, "human": 1}


# --------------------------------------------------------------------------
# The live path
# --------------------------------------------------------------------------
@pytest.fixture
def client(srv):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("ravi", "lead-password", "admin")
    c = srv.app.test_client()
    c.post("/api/login", json={"username": "ravi", "password": "lead-password"})
    return c


def test_pressing_use_this_reply_marks_the_message_hybrid(srv, client, monkeypatch):
    """The console says outright that the draft was taken, so this does not
    depend on the similarity check agreeing."""
    srv.session.last_customer_message = "my recharge failed"
    srv.session.last_suggestion = DRAFT

    scored = type("F", (), {"tone_score": 8, "empathy_score": 7,
                            "clarity_score": 9, "coaching_tip": "good"})()

    monkeypatch.setattr(srv, "over_budget", lambda: None)
    monkeypatch.setattr(srv.session.coach, "evaluate_agent_response",
                        lambda *a, **k: scored)
    monkeypatch.setattr(srv.session.coach, "suggest_reply",
                        lambda *a, **k: "next draft")

    client.post("/api/agent", json={"text": "Totally my own wording here.",
                                    "used_suggestion": True})

    sources = [m.source for m in srv.session.state.history
               if m.speaker == "agent"]
    assert sources == ["hybrid"]


# --------------------------------------------------------------------------
# The split says how much of the closed work it describes
# --------------------------------------------------------------------------
def test_the_split_reports_what_it_covers(srv, case, client):
    """16 shares out of 30 closed cases must not read as all of them."""
    srv.save_case(case(id="SC-1", status="resolved", resolution_mode="ai_autonomous"))
    srv.save_case(case(id="SC-2", status="resolved", resolution_mode="human"))
    # closed, but nobody ever replied -- so no resolver
    srv.save_case(case(id="SC-3", status="resolved", messages=[msg("customer", "hi")]))
    srv.save_case(case(id="SC-4", status="resolved", messages=[msg("customer", "hello")]))

    stats = client.get("/api/stats").get_json()
    assert stats["modes_counted"] == 2       # what the bars describe
    assert stats["modes_eligible"] == 4      # what could have been described


def test_eligible_counts_every_closed_case(srv, case, client):
    srv.save_case(case(id="SC-1", status="resolved", resolution_mode="human"))
    srv.save_case(case(id="SC-2", status="pending"))        # still open
    stats = client.get("/api/stats").get_json()
    assert stats["modes_eligible"] == 1
