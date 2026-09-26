"""Accounts, roles and sessions for the AI Support Coach.

Until this existed, anyone who could reach the port could read every saved
customer transcript. That is a hole on its own, and it sits badly next to a
product whose whole pitch includes masking personal data before it leaves the
machine: there is little point hiding a phone number from Google if the page
showing it is open to the internet.

Three roles, and three workspaces:

    customer  raises and tracks their own tickets, in /portal
    agent     handles escalated cases, in the console at /
    admin     everything an agent has, plus the dashboard, the write-action
              gate -- deciding a refund may actually be issued -- and exports

The STAFF ordering is deliberate: admin is a superset of agent, so a check is
"at least this role" rather than a set membership test, and there is no way to
hold a permission without holding the lesser ones it implies.

Customer is NOT part of that ladder. A customer is not a junior agent -- they
are a different audience who must be kept OUT of the console, not let in with
fewer buttons. They sit at rank 0, which is what makes every staff guard
exclude them for free, and /portal is guarded by require_exact() rather than a
floor so that staff do not land in the customer's workspace either.

Passwords are stored as scrypt hashes by way of werkzeug, which ships with
Flask -- no new dependency, and no home-made cryptography.
"""

import contextlib
import os
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta

from flask import jsonify, redirect, request
from flask_login import LoginManager, UserMixin, current_user
from werkzeug.security import check_password_hash, generate_password_hash

# --------------------------------------------------------------------------
# Roles
# --------------------------------------------------------------------------
ROLES = ("customer", "agent", "admin")

# Rank, not a permission set: "lead or better" is the question every check
# actually asks, and expressing it as a number makes an accidental gap in the
# hierarchy impossible.
# Rank 0 for customer: below every staff role, so at_least("agent") is
# already False for them and the staff guards needed no edits at all.
RANK = {"customer": 0, "agent": 1, "admin": 2}

ROLE_SUMMARY = {
    "customer": "their own tickets, in the portal",
    "agent":    "the console, and the cases they own",
    "admin":    "agent, plus the dashboard, the write-action gate and exports",
}

# Where each role belongs when it signs in, and where a wrong turn sends it
# back to. There is no 403 page: you are returned to your own workspace.
LANDING = {"customer": "/portal", "agent": "/", "admin": "/dashboard"}

# Five wrong passwords parks the account for fifteen minutes. Enough to make
# guessing pointless, short enough that a locked-out agent on a shift is not
# waiting on somebody with a database client.
LOCKOUT_THRESHOLD = 5
LOCKOUT_MINUTES = 15

# How long a login lasts before it has to be done again.
SESSION_HOURS = 12


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT UNIQUE NOT NULL,
    email              TEXT,
    preferred_language TEXT,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    display_name  TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT,
    last_login_at TEXT,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until  TEXT
);
CREATE INDEX IF NOT EXISTS users_username ON users(username);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------
# The case store owns the database path, and the test suite repoints it at a
# temp file AFTER importing the server. So we hold the server's connect()
# itself rather than a path -- it reads the current path on every call, which
# means the tests get their temp database without this module knowing.
_connect = None

login_manager = LoginManager()
login_manager.session_protection = "strong"


def configure(connect):
    """Hand this module the case store's connection factory."""
    global _connect
    _connect = connect


def connect():
    if _connect is None:
        raise RuntimeError("auth.configure(connect) was never called")
    return _connect()


# CREATE TABLE IF NOT EXISTS does nothing to a table that already exists, so
# a database made before email existed needs the column adding by hand.
USER_MIGRATIONS = [
    ("email", "ALTER TABLE users ADD COLUMN email TEXT"),
    # The language a customer is answered in, remembered across sessions.
    ("preferred_language",
     "ALTER TABLE users ADD COLUMN preferred_language TEXT"),
]


def apply_migrations(conn):
    """Add columns an older database is missing, then index them.

    The index is created HERE rather than in SCHEMA because on an existing
    database CREATE TABLE IF NOT EXISTS does nothing, so an index declared
    alongside it would reference a column that has not been added yet.
    """
    have = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if not have:
        return                      # no users table yet; SCHEMA makes it

    for column, statement in USER_MIGRATIONS:
        if column not in have:
            conn.execute(statement)

    conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS users_email
                    ON users(email) WHERE email IS NOT NULL""")


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        apply_migrations(conn)


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# The user
# --------------------------------------------------------------------------
class User(UserMixin):
    def __init__(self, row):
        self.id = str(row["id"])
        self.username = row["username"]
        self.role = row["role"]
        self.display_name = row["display_name"] or row["username"]
        # sqlite3.Row raises rather than returning None for a column that is
        # not in the result, and `in row` would test its VALUES, not its keys.
        try:
            self.email = row["email"]
        except (IndexError, KeyError):
            self.email = None      # a row from a database not yet migrated
        try:
            self.preferred_language = row["preferred_language"] or "en"
        except (IndexError, KeyError):
            self.preferred_language = "en"
        self.active = bool(row["active"])
        self.last_login_at = row["last_login_at"]

    @property
    def is_active(self):
        # Flask-Login refuses to log in a user this returns False for, so
        # deactivating an account is enough to end their access.
        return self.active

    def at_least(self, role):
        # -1 for an unknown role, not 0 -- a role we do not recognise must
        # rank BELOW customer rather than level with it.
        return RANK.get(self.role, -1) >= RANK[role]

    @property
    def landing(self):
        """Where this user belongs, and where a wrong turn returns them."""
        return LANDING.get(self.role, "/")

    def as_dict(self):
        return {
            "username": self.username,
            "role": self.role,
            "display_name": self.display_name,
            "email": self.email,
            "language": self.preferred_language,
            "landing": self.landing,
            "is_customer": self.role == "customer",
            # All four moved up from lead, which no longer exists. Approving a
            # refund is an admin's call now.
            "can_see_dashboard": self.at_least("admin"),
            "can_allow_writes": self.at_least("admin"),
            "can_export": self.at_least("admin"),
            "can_manage_users": self.at_least("admin"),
        }


@login_manager.user_loader
def load_user(user_id):
    try:
        with connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    except sqlite3.Error:
        return None
    return User(row) if row else None


# --------------------------------------------------------------------------
# Accounts
# --------------------------------------------------------------------------
def user_count():
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def list_users():
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM users ORDER BY id").fetchall()
    return [
        {"username": r["username"], "role": r["role"],
         "display_name": r["display_name"] or r["username"],
         "active": bool(r["active"]), "created_at": r["created_at"],
         "last_login_at": r["last_login_at"],
         "locked": _lock_remaining(r["locked_until"]) > 0}
        for r in rows
    ]


def find_user(handle):
    """Look somebody up by email OR username.

    The login page asks for an email, but --add-user still creates accounts
    by username and the test suite signs in with one, so both have to work.
    """
    handle = str(handle or "").strip().lower()
    if not handle:
        return None
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM users WHERE username = ? OR lower(email) = ?",
            (handle, handle)).fetchone()


def create_user(username, password, role, display_name=None, email=None):
    """Add an account. Raises ValueError on anything the caller got wrong."""
    username = str(username).strip().lower()

    if not username:
        raise ValueError("A username is required.")
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    if find_user(username):
        raise ValueError(f"User {username!r} already exists.")

    email = (email or "").strip().lower() or None
    if email and find_user(email):
        raise ValueError(f"{email} is already on another account.")

    with connect() as conn:
        conn.execute("""
            INSERT INTO users (username, email, password_hash, role,
                               display_name, active, created_at)
            VALUES (?, ?, ?, ?, ?, 1, ?)
        """, (username, email, generate_password_hash(password), role,
              display_name or username.title(), now_iso()))

    return username


def set_role(username, role):
    """Move an existing account to another role."""
    if role not in ROLES:
        raise ValueError(f"Role must be one of: {', '.join(ROLES)}.")
    if not find_user(username):
        raise ValueError(f"No user {username!r}.")
    with connect() as conn:
        conn.execute("UPDATE users SET role = ? WHERE username = ?",
                     (role, str(username).strip().lower()))


def set_language(username, code):
    """Remember the language this person wants to be answered in."""
    if not find_user(username):
        raise ValueError(f"No user {username!r}.")
    with connect() as conn:
        conn.execute("UPDATE users SET preferred_language = ? WHERE username = ?",
                     (code, str(username).strip().lower()))


def set_email(username, email):
    """Give an existing account an email address to sign in with."""
    email = (email or "").strip().lower() or None
    existing = find_user(email) if email else None
    if existing and existing["username"] != str(username).strip().lower():
        raise ValueError(f"{email} is already on another account.")
    if not find_user(username):
        raise ValueError(f"No user {username!r}.")
    with connect() as conn:
        conn.execute("UPDATE users SET email = ? WHERE username = ?",
                     (email, str(username).strip().lower()))


def set_password(username, password):
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters.")
    if not find_user(username):
        raise ValueError(f"No user {username!r}.")

    with connect() as conn:
        conn.execute("""
            UPDATE users SET password_hash = ?, failed_logins = 0,
                             locked_until = NULL
            WHERE username = ?
        """, (generate_password_hash(password), str(username).strip().lower()))


def set_active(username, active):
    if not find_user(username):
        raise ValueError(f"No user {username!r}.")
    with connect() as conn:
        conn.execute("UPDATE users SET active = ? WHERE username = ?",
                     (1 if active else 0, str(username).strip().lower()))


# --------------------------------------------------------------------------
# Signing in
# --------------------------------------------------------------------------
def _lock_remaining(locked_until):
    """Seconds left on a lockout, or 0. Never negative."""
    if not locked_until:
        return 0
    try:
        until = datetime.fromisoformat(locked_until)
    except ValueError:
        return 0
    return max(0, int((until - datetime.now(UTC)).total_seconds()))


def authenticate(username, password):
    """Check a login.

    Returns (User, None) on success, or (None, reason) on failure. The reason
    is deliberately vague about WHICH half was wrong -- telling an attacker
    that a username exists is telling them half the answer.
    """
    row = find_user(username)

    if row is None:
        # Hash anyway. Returning instantly for an unknown user is a timing
        # side channel that leaks exactly which usernames are real.
        generate_password_hash(password or "x")
        return None, "Wrong email/username or password."

    locked = _lock_remaining(row["locked_until"])
    if locked:
        return None, (f"Too many failed attempts. Try again in "
                      f"{max(1, round(locked / 60))} minute(s).")

    if not row["active"]:
        return None, "That account has been deactivated."

    if not check_password_hash(row["password_hash"], password or ""):
        _record_failure(row)
        return None, "Wrong email/username or password."

    with connect() as conn:
        conn.execute("""
            UPDATE users SET last_login_at = ?, failed_logins = 0,
                             locked_until = NULL
            WHERE id = ?
        """, (now_iso(), row["id"]))

    return User(find_user(row["username"])), None


def _record_failure(row):
    failures = (row["failed_logins"] or 0) + 1
    locked_until = None

    if failures >= LOCKOUT_THRESHOLD:
        locked_until = (datetime.now(UTC)
                        + timedelta(minutes=LOCKOUT_MINUTES)
                        ).isoformat(timespec="seconds")
        failures = 0        # the lock replaces the count

    with connect() as conn:
        conn.execute(
            "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
            (failures, locked_until, row["id"]))


# --------------------------------------------------------------------------
# Guarding a route
# --------------------------------------------------------------------------
def _denied(message, status):
    """JSON for the API, a redirect for a page.

    The front ends are fetch()-driven, so an API route answering a logged-out
    request with a login PAGE would hand the JavaScript an HTML document where
    it expected JSON, and the error would surface as a parse failure three
    steps from the actual cause.
    """
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": message,
                        "auth": "required" if status == 401 else "forbidden"}), status
    if status == 401:
        return redirect("/login?next=" + request.path)

    # A page this role cannot open sends them to their OWN workspace rather
    # than to a dead end. Telling a customer they lack a permission they were
    # never going to be given is a worse answer than simply taking them home.
    try:
        return redirect(current_user.landing)
    except AttributeError:
        return redirect("/login")


@login_manager.unauthorized_handler
def unauthorized():
    return _denied("Sign in to continue.", 401)


def require_exact(*allowed):
    """Decorator: this route needs one of exactly these roles.

    require_role() is a FLOOR, and a floor is wrong for /portal: admin
    outranks customer, so "at least customer" would let staff into the
    customer's workspace. Audience, not seniority, so membership not rank.
    """
    def decorate(view):
        from functools import wraps

        @wraps(view)
        def guarded(*args, **kwargs):
            if not current_user.is_authenticated:
                return _denied("Sign in to continue.", 401)
            if current_user.role not in allowed:
                return _denied(
                    f"This is for {' or '.join(allowed)}. You are signed in "
                    f"as {current_user.role}.", 403)
            return view(*args, **kwargs)

        return guarded

    return decorate


def require_role(minimum):
    """Decorator: this route needs `minimum` or better.

    Used instead of Flask-Login's @login_required everywhere, because every
    route in this app has a role floor -- there is no page that merely needs
    "somebody, anybody" -- and one decorator is harder to forget than two.
    """
    def decorate(view):
        from functools import wraps

        @wraps(view)
        def guarded(*args, **kwargs):
            if not current_user.is_authenticated:
                return _denied("Sign in to continue.", 401)
            if not current_user.at_least(minimum):
                return _denied(
                    f"This needs the {minimum} role. You are signed in as "
                    f"{current_user.role}.", 403)
            return view(*args, **kwargs)

        return guarded

    return decorate


# --------------------------------------------------------------------------
# Secret key
# --------------------------------------------------------------------------
def secret_key():
    """The key that signs session cookies.

    Generated once and kept in the database rather than made fresh per boot:
    a new key every restart silently signs everybody out, which during a demo
    looks exactly like a bug.
    """
    from_env = os.getenv("SECRET_KEY")
    if from_env:
        return from_env

    with connect() as conn:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'secret_key'").fetchone()
        if row:
            return row["value"]

        key = secrets.token_hex(32)
        conn.execute(
            "INSERT INTO settings (key, value) VALUES ('secret_key', ?)", (key,))
        return key


def harden(app, local_only=True):
    """Session cookie settings.

    SameSite=Lax is what stands between this and cross-site request forgery:
    the API is cookie-authenticated, so without it any page on the internet
    could POST to /api/resolve in a logged-in agent's name. Lax stops a
    cross-site POST from carrying the cookie at all.

    It is not the same thing as per-request CSRF tokens, which is the stronger
    answer and the next thing to add here.
    """
    app.config.update(
        SECRET_KEY=secret_key(),
        SESSION_COOKIE_HTTPONLY=True,       # JavaScript cannot read it
        SESSION_COOKIE_SAMESITE="Lax",      # not sent on a cross-site POST
        SESSION_COOKIE_SECURE=not local_only,   # HTTPS only, once deployed
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
    )
    login_manager.init_app(app)


# --------------------------------------------------------------------------
# First run
# --------------------------------------------------------------------------
def ensure_seed_admin():
    """Create the first admin if the table is empty.

    Returns (username, password) when it made one -- the password is shown
    once in the startup banner and never stored in the clear -- or None when
    accounts already exist.

    Called only from __main__, never from init_db(), so importing the server
    (which the test suite does per test) can never create an account.
    """
    if user_count():
        return None

    username = os.getenv("ADMIN_USERNAME", "admin").strip().lower()
    password = os.getenv("ADMIN_PASSWORD")
    generated = not password

    if generated:
        # Readable enough to retype from a terminal, random enough to keep.
        password = secrets.token_urlsafe(12)

    create_user(username, password, "admin", display_name="Administrator",
                email=os.getenv("ADMIN_EMAIL", "admin@support-coach.local"))
    return (username, password if generated else None)


# The three accounts the login page's demo buttons sign in as. Created only
# when an account of that role does not already exist, so running this twice
# does nothing and an account somebody renamed is left alone.
DEMO_USERS = [
    ("priya", "Priya Sharma", "customer", "priya@support-coach.local"),
    ("rahul", "Rahul Verma",  "agent",    "rahul@support-coach.local"),
]


def ensure_demo_users(password):
    """Seed the customer and agent the demo buttons expect.

    Returns the usernames it created. The admin comes from
    ensure_seed_admin(), which runs first and owns the generated password.
    """
    made = []
    for username, display, role, email in DEMO_USERS:
        row = find_user(username)
        if row is None:
            create_user(username, password, role, display_name=display,
                        email=email)
            made.append(username)
        elif not row["email"]:
            # An account made before the email column existed has none, so
            # the demo buttons -- which sign in BY email -- could not reach
            # it. Give it the address, and nothing else: the password stays
            # whatever its owner set.
            backfill_email(username, email)

    # The seeded admin has the same history.
    admin = os.getenv("ADMIN_USERNAME", "admin").strip().lower()
    row = find_user(admin)
    if row is not None and not row["email"]:
        backfill_email(admin, os.getenv("ADMIN_EMAIL", "admin@support-coach.local"))
    return made


def backfill_email(username, email):
    """Set an email only if it is free. Never raises: a clash just skips."""
    with contextlib.suppress(ValueError):
        set_email(username, email)
