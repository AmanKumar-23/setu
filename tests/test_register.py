"""Self-service sign-up from the login page.

The one thing these exist to defend: a form cannot hand anybody a role. The
console is as far as sign-up goes, and the dashboard, the write-action gate
and the exports stay behind an account somebody made deliberately.
"""

import pytest


@pytest.fixture
def client(srv):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("ravi", "lead-password", "lead")
    return srv.app.test_client()


def signup(client, **over):
    body = {"username": "newperson", "password": "a-good-password",
            "confirm": "a-good-password"}
    body.update(over)
    return client.post("/api/register", json=body)


# --------------------------------------------------------------------------
# The role floor
# --------------------------------------------------------------------------
def test_a_new_account_is_an_agent(client):
    body = signup(client).get_json()
    assert body["ok"] is True
    assert body["user"]["role"] == "agent"


def test_a_smuggled_role_is_ignored(client):
    """Sending role=admin in the payload must change nothing."""
    body = signup(client, username="sneaky", role="admin").get_json()
    assert body["user"]["role"] == "agent"
    assert body["user"]["can_see_dashboard"] is False
    assert body["user"]["can_export"] is False
    assert body["user"]["can_manage_users"] is False


def test_a_new_account_cannot_reach_the_dashboard(client):
    signup(client, username="fresh")
    assert client.get("/api/stats").status_code == 403
    assert client.get("/api/cases.csv").status_code == 403
    assert client.get("/").status_code == 200        # the console is theirs


def test_signing_up_signs_you_in_and_lands_on_the_console(client):
    body = signup(client, username="lands").get_json()
    assert body["next"] == "/"
    assert client.get("/api/me").get_json()["user"]["username"] == "lands"


# --------------------------------------------------------------------------
# What it refuses
# --------------------------------------------------------------------------
def test_a_duplicate_username_is_refused(client):
    signup(client, username="taken")
    again = signup(client, username="taken")
    assert again.status_code == 400
    assert "already exists" in again.get_json()["error"]


def test_an_existing_account_cannot_be_overwritten(client):
    """Signing up as an existing lead must not downgrade or reset them."""
    reply = signup(client, username="ravi", password="hijack-attempt",
                   confirm="hijack-attempt")
    assert reply.status_code == 400
    assert client.post("/api/login",
                       json={"username": "ravi",
                             "password": "lead-password"}).status_code == 200


def test_a_short_password_is_refused(client):
    reply = signup(client, password="short", confirm="short")
    assert reply.status_code == 400
    assert "8 characters" in reply.get_json()["error"]


def test_mismatched_passwords_are_refused(client):
    reply = signup(client, confirm="something-else")
    assert reply.status_code == 400
    assert "do not match" in reply.get_json()["error"]


@pytest.mark.parametrize("name", ["ab", "has space", "bad!char", "x" * 40, ""])
def test_a_badly_shaped_username_is_refused(client, name):
    assert signup(client, username=name).status_code == 400


def test_usernames_are_stored_lowercase(client):
    body = signup(client, username="MixedCase").get_json()
    assert body["user"]["username"] == "mixedcase"


def test_the_display_name_is_optional(client):
    body = signup(client, username="noname").get_json()
    assert body["ok"] is True
    assert body["user"]["display_name"]
