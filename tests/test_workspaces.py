"""Three roles, three workspaces, and the walls between them.

The rule these defend is not "admin can do more". It is that a customer and
a member of staff belong in DIFFERENT places -- so the guard on /portal is
membership, not rank, and an admin is kept out of it just as firmly as a
customer is kept out of the console.
"""

import pytest


@pytest.fixture
def auth_mod(srv):
    import auth
    return auth


@pytest.fixture
def client(srv, auth_mod):
    auth_mod.harden(srv.app, local_only=True)
    srv.app.config["TESTING"] = True
    auth_mod.create_user("priya", "customer-pw", "customer",
                         email="priya@support-coach.local")
    auth_mod.create_user("rahul", "agent-pw", "agent",
                         email="rahul@support-coach.local")
    auth_mod.create_user("root", "admin-pw", "admin",
                         email="admin@support-coach.local")
    return srv.app.test_client()


def sign_in(client, handle, password, role=None):
    body = {"email": handle, "password": password}
    if role:
        body["role"] = role
    return client.post("/api/login", json=body)


# --------------------------------------------------------------------------
# Landing
# --------------------------------------------------------------------------
@pytest.mark.parametrize("handle,password,where", [
    ("priya@support-coach.local", "customer-pw", "/portal"),
    ("rahul@support-coach.local", "agent-pw", "/"),
    ("admin@support-coach.local", "admin-pw", "/dashboard"),
])
def test_each_role_lands_in_its_own_workspace(client, handle, password, where):
    assert sign_in(client, handle, password).get_json()["next"] == where


def test_signing_in_by_username_still_works(client):
    """--add-user makes accounts by username, and the suite uses them."""
    assert sign_in(client, "rahul", "agent-pw").get_json()["ok"] is True


# --------------------------------------------------------------------------
# The walls
# --------------------------------------------------------------------------
def test_a_customer_cannot_reach_the_console_or_the_dashboard(client):
    sign_in(client, "priya@support-coach.local", "customer-pw")
    for path in ("/", "/dashboard"):
        assert client.get(path).status_code == 302
    for path in ("/api/queue", "/api/state", "/api/stats", "/api/cases"):
        assert client.get(path).status_code == 403, path


def test_a_customer_reaches_their_own_portal(client):
    sign_in(client, "priya@support-coach.local", "customer-pw")
    assert client.get("/portal").status_code == 200


@pytest.mark.parametrize("handle,password,home", [
    ("rahul@support-coach.local", "agent-pw", "/"),
    ("admin@support-coach.local", "admin-pw", "/dashboard"),
])
def test_staff_are_kept_out_of_the_portal(client, handle, password, home):
    """An admin outranks a customer and is still not one. A rank floor would
    have let them straight in."""
    sign_in(client, handle, password)
    res = client.get("/portal")
    assert res.status_code == 302
    assert res.headers["Location"].endswith(home)


def test_nobody_is_shown_a_403_page(client):
    """Wrong role means "go home", never a dead end."""
    sign_in(client, "rahul@support-coach.local", "agent-pw")
    assert client.get("/dashboard").headers["Location"].endswith("/")
    assert client.get("/denied").status_code == 404      # the page is gone


# --------------------------------------------------------------------------
# The role card
# --------------------------------------------------------------------------
def test_the_role_card_must_match_the_account(client):
    """Picking Admin does not make you one -- it is checked, not trusted."""
    reply = sign_in(client, "priya@support-coach.local", "customer-pw",
                    role="admin")
    assert reply.status_code == 403
    assert "customer account" in reply.get_json()["error"]


def test_the_matching_role_card_signs_you_in(client):
    reply = sign_in(client, "priya@support-coach.local", "customer-pw",
                    role="customer")
    assert reply.get_json()["ok"] is True


def test_no_card_at_all_still_signs_you_in(client):
    assert sign_in(client, "priya@support-coach.local", "customer-pw"
                   ).get_json()["ok"] is True


# --------------------------------------------------------------------------
# The retired role
# --------------------------------------------------------------------------
def test_lead_is_gone(auth_mod):
    assert "lead" not in auth_mod.ROLES
    with pytest.raises(ValueError):
        auth_mod.create_user("someone", "a-password", "lead")


def test_an_account_still_on_lead_is_moved_to_admin(srv, auth_mod):
    """It would fail every guard and be unable to sign in anywhere."""
    with auth_mod.connect() as conn:
        conn.execute("INSERT INTO users (username, password_hash, role, active,"
                     " created_at) VALUES ('old', 'x', 'lead', 1, '2026-01-01')")
    moved = srv.migrate_retired_roles()
    assert ("old", "lead", "admin") in moved
    assert auth_mod.find_user("old")["role"] == "admin"
