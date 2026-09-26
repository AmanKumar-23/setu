"""The customer's profile page.

Everything it shows is read from the users table -- the same row sign-in
checks -- and the fields that can change are the ones that are safe to
change without a verification step.
"""

import pytest


@pytest.fixture
def client(srv):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("priya", "customer-pw", "customer",
                     display_name="Priya Sharma", email="priya@example.com")
    auth.create_user("rahul", "agent-pwxx", "agent")
    client = srv.app.test_client()
    client.post("/api/login", json={"username": "priya", "password": "customer-pw"})
    return client


def test_the_page_is_there_for_a_customer(client):
    assert client.get("/portal/profile").status_code == 200


def test_it_shows_the_persisted_account(client):
    p = client.get("/api/portal/profile").get_json()["profile"]
    assert p["display_name"] == "Priya Sharma"
    assert p["email"] == "priya@example.com"
    assert p["username"] == "priya"
    assert p["member_since"]                   # captured at sign-up
    assert p["last_sign_in"]                   # set by the sign-in just now


def test_name_and_phone_are_saved_to_the_database(client):
    import auth
    d = client.post("/api/portal/profile", json={
        "display_name": "  Priya   S. ", "phone": "+91 98765 43210"}).get_json()
    assert d["ok"] is True
    row = auth.find_user("priya")
    assert row["display_name"] == "Priya S."          # spacing tidied
    assert row["phone"] == "+91 98765 43210"
    # ...and it survives signing out and back in
    client.post("/api/logout")
    client.post("/api/login", json={"username": "priya", "password": "customer-pw"})
    assert client.get("/api/portal/profile").get_json()["profile"]["phone"] == "+91 98765 43210"


def test_the_phone_can_be_cleared(client):
    client.post("/api/portal/profile", json={"phone": "9876543210"})
    client.post("/api/portal/profile", json={"phone": ""})
    assert client.get("/api/portal/profile").get_json()["profile"]["phone"] == ""


@pytest.mark.parametrize("body,code", [
    ({"display_name": ""}, "name"),
    ({"display_name": "x" * 61}, "name"),
    ({"phone": "12345"}, "phone"),
    ({"phone": "call me maybe"}, "phone"),
])
def test_bad_values_are_refused_with_a_code(client, body, code):
    d = client.post("/api/portal/profile", json=body)
    assert d.status_code == 400
    assert d.get_json()["code"] == code        # the page shows it translated


def test_email_and_username_cannot_be_changed_from_here(client):
    """Read-only by design: the email is the sign-in identifier, and the
    username is what every ticket is filed under."""
    import auth
    client.post("/api/portal/profile", json={
        "email": "attacker@example.com", "username": "admin",
        "display_name": "Priya"})
    row = auth.find_user("priya")
    assert row["email"] == "priya@example.com"
    assert auth.find_user("attacker@example.com") is None


def test_the_password_changes_only_with_the_current_one(client):
    import auth
    wrong = client.post("/api/portal/password", json={
        "current": "not-it", "new": "a-new-password", "confirm": "a-new-password"})
    assert wrong.status_code == 400 and wrong.get_json()["code"] == "current"

    ok = client.post("/api/portal/password", json={
        "current": "customer-pw", "new": "a-new-password", "confirm": "a-new-password"})
    assert ok.get_json()["ok"] is True
    assert auth.authenticate("priya", "a-new-password")[0] is not None
    assert auth.authenticate("priya", "customer-pw")[0] is None


@pytest.mark.parametrize("body,code", [
    ({"current": "customer-pw", "new": "a-new-password", "confirm": "different"}, "mismatch"),
    ({"current": "customer-pw", "new": "short", "confirm": "short"}, "short"),
    ({"current": "customer-pw", "new": "customer-pw", "confirm": "customer-pw"}, "same"),
])
def test_password_refusals_each_say_which(client, body, code):
    d = client.post("/api/portal/password", json=body)
    assert d.status_code == 400 and d.get_json()["code"] == code


def test_signing_out_ends_the_session(client):
    client.post("/api/logout")
    assert client.get("/api/portal/profile").status_code in (302, 401)
    page = client.get("/portal/profile")
    assert page.status_code == 302 and "/login" in page.headers["Location"]


def test_an_agent_has_no_customer_profile_page(srv):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("rahul", "agent-pwxx", "agent")
    agent = srv.app.test_client()
    agent.post("/api/login", json={"username": "rahul", "password": "agent-pwxx"})
    assert agent.get("/api/portal/profile").status_code in (302, 403)


def test_every_profile_error_has_a_translation_key(srv):
    for code in ("name", "phone", "current", "same", "short", "mismatch"):
        assert f"profile.err.{code}" in srv.UI_STRINGS
