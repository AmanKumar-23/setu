"""The human gate on data-changing actions.

The claim this file exists to defend is a narrow one and it is absolute: no
write tool runs unless a person clicked Approve. Not on high confidence, not
on low risk, not when the checkbox is ticked, never.

Two separate mechanisms back it, and both are tested here:

  1. with actions off, the write DECLARATIONS are never sent to the API, so
     the model has no function to call; and
  2. the function-calling loop never executes a write even when they ARE
     sent -- it records a proposal and moves on.

The second is what makes the first a defence in depth rather than the only
thing standing there.
"""

import json

import pytest


@pytest.fixture
def coach(core):
    obj = object.__new__(core.AICoach)
    obj.last_redactions = []
    obj.last_article = None
    obj.model = "gemini-3.5-flash"
    obj.client = None
    return obj


class FakeCall:
    def __init__(self, name, args=None):
        self.name = name
        self.args = args or {}


class FakeContent:
    """Stands in for response.candidates[0].content."""
    def __init__(self, role="model"):
        self.role = role
        self.parts = []


class FakeResponse:
    def __init__(self, calls):
        self.function_calls = calls
        self.candidates = [type("C", (), {"content": FakeContent()})()]


# --------------------------------------------------------------------------
# 1. The tool list itself
# --------------------------------------------------------------------------
def test_with_actions_off_the_write_tools_are_not_offered_at_all(core):
    """Not "offered and refused" -- not sent."""
    offered = {d.name for d in core.tools_for(False)[0].function_declarations}
    assert offered == {"check_order_status", "check_refund_status"}
    for name in core.WRITE_TOOLS:
        assert name not in offered


def test_with_actions_on_the_write_tools_are_offered(core):
    offered = {d.name for d in core.tools_for(True)[0].function_declarations}
    assert offered >= core.WRITE_TOOLS


# --------------------------------------------------------------------------
# 2. The loop never executes a write -- the non-negotiable
# --------------------------------------------------------------------------
@pytest.mark.parametrize("tool_name,args", [
    ("initiate_refund", {"order_id": "OD-4468", "amount": 349}),
    ("expedite_delivery", {"order_id": "OD-4468"}),
    ("reset_account_access", {"customer_id": "CU-1001"}),
])
def test_the_model_cannot_execute_a_write_even_with_actions_on(
    core, coach, back_office, monkeypatch, tool_name, args
):
    """allow_writes=True, the model asks outright, and nothing happens."""
    before = json.dumps(back_office, sort_keys=True)
    rounds = []

    def fake_call(contents, config=None, max_attempts=3, operation="generate"):
        rounds.append(1)
        if len(rounds) == 1:
            return FakeResponse([FakeCall(tool_name, args)])
        return FakeResponse([])

    monkeypatch.setattr(coach, "_call_model", fake_call)

    facts = coach.gather_facts("just refund me", allow_writes=True)

    assert json.dumps(back_office, sort_keys=True) == before, (
        f"{tool_name} changed the data store without anyone approving it")

    entry = next(f for f in facts if f["name"] == tool_name)
    assert entry["ran"] is False
    assert entry["proposed"] is True
    assert entry["result"]["status"] == "proposed"


def test_a_read_tool_still_runs_by_itself(core, coach, back_office, monkeypatch):
    """The gate is on writes only -- reads must not have become useless."""
    rounds = []

    def fake_call(contents, config=None, max_attempts=3, operation="generate"):
        rounds.append(1)
        if len(rounds) == 1:
            return FakeResponse([FakeCall("check_order_status",
                                          {"order_id": "OD-4468"})])
        return FakeResponse([])

    monkeypatch.setattr(coach, "_call_model", fake_call)
    facts = coach.gather_facts("where is my order", allow_writes=False)

    entry = next(f for f in facts if f["name"] == "check_order_status")
    assert entry["ran"] is True
    assert entry["result"]["status"] == "in transit"


# --------------------------------------------------------------------------
# 3. The audit trail
# --------------------------------------------------------------------------
@pytest.fixture
def trail(srv):
    import actions
    return actions


def test_a_proposal_is_recorded_before_anyone_decides(trail):
    row = trail.propose("SC-1001", "rahul", "initiate_refund",
                        {"order_id": "OD-4471", "amount": 499}, reason="failed recharge")
    assert row["decision"] == "pending"
    assert row["case_id"] == "SC-1001"
    assert row["username"] == "rahul"
    assert row["at"]
    assert row["description"] == "Refund INR 499 on order OD-4471"


def test_arguments_are_described_in_plain_english(trail):
    assert trail.describe("initiate_refund", {"order_id": "OD-4471"}) == \
        "Refund the full amount on order OD-4471"
    assert trail.describe("expedite_delivery", {"order_id": "OD-4468"}) == \
        "Expedite delivery on order OD-4468"
    assert "sign-in link" in trail.describe("reset_account_access", {})


def test_a_decision_records_who_and_when(trail):
    row = trail.propose("SC-1001", "priya", "expedite_delivery",
                        {"order_id": "OD-4468"})
    decided, why = trail.decide(row["id"], trail.APPROVED, "ravi")
    assert why is None
    assert decided["decision"] == "approved"
    assert decided["decided_by"] == "ravi"
    assert decided["decided_at"]


def test_the_same_proposal_cannot_be_decided_twice(trail):
    """A double-clicked Approve must not issue two refunds."""
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4471"})
    trail.decide(row["id"], trail.APPROVED, "ravi")
    again, why = trail.decide(row["id"], trail.APPROVED, "ravi")
    assert again is None
    assert "already approved" in why


def test_rejected_proposals_stay_in_the_trail(trail):
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4471"})
    trail.decide(row["id"], trail.REJECTED, "ravi")
    assert trail.totals()["rejected"] == 1
    assert [r["decision"] for r in trail.for_case("SC-1001")] == ["rejected"]


def test_the_trail_filters_like_the_cases_table(trail):
    trail.propose("SC-1", "priya", "initiate_refund", {"order_id": "OD-1"})
    trail.propose("SC-2", "priya", "expedite_delivery", {"order_id": "OD-2"})
    rejected = trail.propose("SC-3", "priya", "initiate_refund", {"order_id": "OD-3"})
    trail.decide(rejected["id"], trail.REJECTED, "ravi")

    rows, _ = trail.listing(action="initiate_refund")
    assert {r["case_id"] for r in rows} == {"SC-1", "SC-3"}

    rows, _ = trail.listing(decision="rejected")
    assert [r["case_id"] for r in rows] == ["SC-3"]

    rows, _ = trail.listing(query="OD-2")
    assert [r["case_id"] for r in rows] == ["SC-2"]


# --------------------------------------------------------------------------
# 4. Only a person, over HTTP, with the right role, can approve
# --------------------------------------------------------------------------
@pytest.fixture
def client(srv):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("priya", "agent-password", "agent")
    auth.create_user("ravi", "lead-password", "lead")
    return srv.app.test_client()


def sign_in(client, username, password):
    return client.post("/api/login",
                       json={"username": username, "password": password})


def test_a_signed_out_visitor_cannot_approve_anything(client, trail):
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4471"})
    reply = client.post(f"/api/actions/{row['id']}/decide",
                        json={"decision": "approved"})
    assert reply.status_code == 401


def test_an_agent_cannot_arm_write_actions(client):
    sign_in(client, "priya", "agent-password")
    assert client.post("/api/allow-writes", json={"allow": True}).status_code == 403


def test_an_agent_cannot_approve_an_action(client, trail):
    """The role that talks to customers is not the role that spends money."""
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4471"})
    sign_in(client, "priya", "agent-password")
    reply = client.post(f"/api/actions/{row['id']}/decide",
                        json={"decision": "approved"})
    assert reply.status_code == 403
    assert trail.get(row["id"])["decision"] == "pending"


def test_arming_actions_reports_what_is_now_offered(client):
    sign_in(client, "ravi", "lead-password")
    on = client.post("/api/allow-writes", json={"allow": True}).get_json()
    assert on["allow_writes"] is True
    assert set(on["offered"]) == {"initiate_refund", "expedite_delivery",
                                  "reset_account_access"}

    off = client.post("/api/allow-writes", json={"allow": False}).get_json()
    assert off["allow_writes"] is False
    assert off["offered"] == []


def test_approving_runs_the_tool_and_records_the_reference(
    client, trail, back_office
):
    before = len(back_office["refunds"])
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4468", "amount": 349})

    sign_in(client, "ravi", "lead-password")
    reply = client.post(f"/api/actions/{row['id']}/decide",
                        json={"decision": "approved"})

    assert reply.status_code == 200
    done = reply.get_json()["action"]
    assert done["decision"] == "approved"
    assert done["decided_by"] == "ravi"
    assert done["reference"].startswith("RF-")
    assert len(back_office["refunds"]) == before + 1


def test_rejecting_records_the_decision_and_changes_nothing(
    client, trail, back_office
):
    before = json.dumps(back_office, sort_keys=True)
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4468"})

    sign_in(client, "ravi", "lead-password")
    reply = client.post(f"/api/actions/{row['id']}/decide",
                        json={"decision": "rejected"})

    assert reply.status_code == 200
    assert reply.get_json()["action"]["decision"] == "rejected"
    assert json.dumps(back_office, sort_keys=True) == before


def test_approving_twice_runs_the_tool_once(client, trail, back_office):
    before = len(back_office["refunds"])
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4468"})

    sign_in(client, "ravi", "lead-password")
    first = client.post(f"/api/actions/{row['id']}/decide",
                        json={"decision": "approved"})
    second = client.post(f"/api/actions/{row['id']}/decide",
                         json={"decision": "approved"})

    assert first.status_code == 200
    assert second.status_code == 409
    assert len(back_office["refunds"]) == before + 1


def test_a_nonsense_decision_is_refused(client, trail):
    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4468"})
    sign_in(client, "ravi", "lead-password")
    reply = client.post(f"/api/actions/{row['id']}/decide",
                        json={"decision": "approved-ish"})
    assert reply.status_code == 400
    assert trail.get(row["id"])["decision"] == "pending"


def test_the_audit_trail_is_behind_a_role(client, trail):
    trail.propose("SC-1001", "priya", "initiate_refund", {"order_id": "OD-4471"})
    assert client.get("/api/actions").status_code == 401

    sign_in(client, "priya", "agent-password")
    assert client.get("/api/actions").status_code == 403

    sign_in(client, "ravi", "lead-password")
    reply = client.get("/api/actions")
    assert reply.status_code == 200
    assert len(reply.get_json()["actions"]) == 1


def test_only_an_admin_exports_the_trail(client, trail):
    sign_in(client, "ravi", "lead-password")
    assert client.get("/api/actions.csv").status_code == 403


def test_the_export_carries_the_decision_and_who_made_it(srv, trail):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("root", "admin-password", "admin")
    client = srv.app.test_client()

    row = trail.propose("SC-1001", "priya", "initiate_refund",
                        {"order_id": "OD-4471", "amount": 499},
                        reason="failed recharge")
    trail.decide(row["id"], trail.REJECTED, "root")

    client.post("/api/login", json={"username": "root",
                                    "password": "admin-password"})
    reply = client.get("/api/actions.csv")
    assert reply.status_code == 200
    assert "text/csv" in reply.headers["Content-Type"]

    body = reply.get_data(as_text=True)
    header, line = body.splitlines()[0], body.splitlines()[1]
    assert "decision" in header and "decided_by" in header
    # The refused ones are the whole point of keeping the trail.
    assert "rejected" in line
    assert "root" in line
    assert "OD-4471" in line


def test_the_export_honours_the_active_filter(srv, trail):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("root", "admin-password", "admin")
    client = srv.app.test_client()

    trail.propose("SC-1", "priya", "initiate_refund", {"order_id": "OD-1"})
    trail.propose("SC-2", "priya", "expedite_delivery", {"order_id": "OD-2"})

    client.post("/api/login", json={"username": "root",
                                    "password": "admin-password"})
    body = client.get("/api/actions.csv?action=expedite_delivery").get_data(as_text=True)
    assert "SC-2" in body
    assert "SC-1" not in body
