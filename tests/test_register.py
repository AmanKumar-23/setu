"""Self-service sign-up from the login page.

The one thing these exist to defend: a form cannot hand anybody a role.
Sign-up makes a CUSTOMER -- somebody who finds the login page and makes an
account is a person with a problem, not a member of staff -- so the console,
the dashboard, the write-action gate and the exports all stay behind an
account somebody made deliberately with --add-user.
"""

import pytest


@pytest.fixture
def client(srv):
    import auth
    auth.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth.create_user("ravi", "lead-password", "admin")
    return srv.app.test_client()


def signup(client, **over):
    body = {"username": "newperson", "email": "newperson@example.com",
            "password": "a-good-password", "confirm": "a-good-password"}
    body.update(over)
    # Every account needs its own address, so derive one from the username
    # unless the test is deliberately setting it.
    if "email" not in over and "username" in over:
        body["email"] = f"{over['username']}@example.com"
    return client.post("/api/register", json=body)


# --------------------------------------------------------------------------
# The role floor
# --------------------------------------------------------------------------
def test_a_new_account_is_a_customer(client):
    body = signup(client).get_json()
    assert body["ok"] is True
    assert body["user"]["role"] == "customer"


def test_a_smuggled_role_is_ignored(client):
    """Sending role=admin in the payload must change nothing."""
    body = signup(client, username="sneaky", role="admin").get_json()
    assert body["user"]["role"] == "customer"
    assert body["user"]["can_see_dashboard"] is False
    assert body["user"]["can_export"] is False
    assert body["user"]["can_manage_users"] is False


def test_a_new_account_reaches_only_the_portal(client):
    signup(client, username="fresh")
    assert client.get("/api/stats").status_code == 403
    assert client.get("/api/cases.csv").status_code == 403
    assert client.get("/api/queue").status_code == 403     # not the console
    assert client.get("/portal").status_code == 200        # their own workspace


def test_signing_up_signs_you_in_and_lands_on_the_portal(client):
    body = signup(client, username="lands").get_json()
    assert body["next"] == "/portal"
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



# --------------------------------------------------------------------------
# The bug this file was reopened for: the exact credentials used to register
# did not sign back in. The route read the email and then never stored it,
# so the account existed but could only be found by username -- and the
# sign-in form asks for an email.
# --------------------------------------------------------------------------
def login(client, handle, password, role=None):
    body = {"email": handle, "password": password}
    if role:
        body["role"] = role
    return client.post("/api/login", json=body)


def test_signup_logout_login_with_the_same_email_succeeds(client):
    made = signup(client, username="lavyasree", email="lavyalav@gmail.com",
                  password="lavya1234", confirm="lavya1234").get_json()
    assert made["ok"] is True
    assert made["user"]["email"] == "lavyalav@gmail.com"

    client.post("/api/logout")
    assert client.get("/api/me").status_code == 401

    back = login(client, "lavyalav@gmail.com", "lavya1234", role="customer")
    assert back.status_code == 200
    body = back.get_json()
    assert body["ok"] is True
    assert body["next"] == "/portal"           # a customer lands in the portal


def test_the_email_is_actually_persisted(client):
    """Not just echoed back -- it has to be in the users table."""
    import auth
    signup(client, username="stored", email="Stored@Example.com")
    row = auth.find_user("stored")
    assert row["email"] == "stored@example.com"


def test_email_login_ignores_case_and_surrounding_space(client):
    signup(client, username="casey", email="casey@example.com")
    client.post("/api/logout")
    assert login(client, "  CASEY@Example.COM ", "a-good-password").status_code == 200


def test_signing_up_without_an_email_is_refused(client):
    body = signup(client, username="noemail", email="").get_json()
    assert body["ok"] is False
    assert "email" in body["error"].lower()


def test_an_email_already_in_use_is_refused(client):
    signup(client, username="first", email="shared@example.com")
    client.post("/api/logout")
    body = signup(client, username="second", email="shared@example.com").get_json()
    assert body["ok"] is False


def test_a_wrong_password_and_a_wrong_card_get_different_answers(client):
    """A wrong password is vague on purpose. A RIGHT password on the wrong
    card is a different mistake, and says which card to use."""
    signup(client, username="twocards", email="twocards@example.com")
    client.post("/api/logout")

    wrong_pw = login(client, "twocards@example.com", "not-the-password",
                     role="customer")
    assert wrong_pw.status_code == 401
    assert "password" in wrong_pw.get_json()["error"].lower()

    wrong_card = login(client, "twocards@example.com", "a-good-password",
                       role="agent")
    assert wrong_card.status_code == 403
    body = wrong_card.get_json()
    assert body["reason"] == "wrong_account_type"
    assert body["role"] == "customer"          # so the page can select it
    assert "customer" in body["error"].lower()


def test_no_card_at_all_signs_in_by_the_accounts_own_role(client):
    """Nobody should have to guess a card: without one, the account's own
    role decides where they land."""
    signup(client, username="nocard", email="nocard@example.com")
    client.post("/api/logout")
    body = login(client, "nocard@example.com", "a-good-password").get_json()
    assert body["ok"] is True and body["next"] == "/portal"


def test_a_demo_account_made_before_emails_existed_gets_its_address(srv):
    """The demo buttons sign in by email. An account created before the
    column existed had none, so the buttons could never reach it."""
    import auth
    auth.create_user("priya", "whatever-it-was", "customer")      # no email
    assert auth.find_user("priya")["email"] is None

    auth.ensure_demo_users("demo-password-1")

    row = auth.find_user("priya")
    assert row["email"] == "priya@support-coach.local"
    # ...and the password its owner set is untouched.
    user, _ = auth.authenticate("priya@support-coach.local", "whatever-it-was")
    assert user is not None
