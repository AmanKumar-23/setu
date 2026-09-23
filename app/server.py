"""Web backend for the AI Support Coach.

The coaching logic is NOT reimplemented here. It is imported from
coach_core.py, which is generated straight from customer_support_coach.ipynb
by build_core.py. This file only does four things:

    1. hold the state of the conversation currently open,
    2. keep every conversation as a "case" on disk, with a status,
    3. expose all of that over a small JSON API,
    4. serve the two front ends (console + dashboard),
    5. decide who is allowed to see which of those -- see auth.py.

Run it with:

    python3 app/server.py
"""

import contextlib
import csv
import io
import json
import logging
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher

from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from flask_login import current_user, login_user, logout_user

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import actions  # noqa: E402  (needs HERE on the path)
import auth  # noqa: E402
import metering  # noqa: E402
import pipeline  # noqa: E402
from auth import require_exact, require_role  # noqa: E402
from pipeline import note_redactions, open_rating_slot  # noqa: E402

try:
    import coach_core
except ImportError as import_error:
    print(f"\n  Could not load the coaching engine: {import_error}")
    print("  The Python running this server does not have the Gemini SDK.")
    print("  Install it for this interpreter:\n")
    print(f"      {sys.executable} -m pip install google-genai\n")
    raise SystemExit(1) from import_error

# The SDK prints a long advisory about automatic function calling that does
# not apply to us. Quiet it so the server log stays readable.
logging.getLogger("google_genai.models").setLevel(logging.ERROR)

app = Flask(__name__, static_folder=os.path.join(HERE, "static"))

# analyze_customer_message() now returns a real 0-100 frustration score of its
# own, so the old low/medium/high -> number lookup is gone. This stays only as
# a fallback for a response that somehow arrives without one.
# The canned questions offered in the console, so an agent (or you, during a
# demo) can pick a common issue instead of typing it out. The topics mirror
# the KNOWLEDGE_BASE categories in Part 1 of the notebook, and the keywords
# are what we count cases against for the dashboard's volume figures.
# The six quick-fill chips in the console. Their `text` is the AGENT's
# opening line, not the customer's complaint: customers write their own
# messages in their own workspace now, so a chip that filled the box with
# "mera recharge nahi hua" would be putting words in the wrong mouth.
#
# `keywords` still describe the CUSTOMER's language, because that is what the
# dashboard's Knowledge section matches saved cases against.
FAQS = [
    {
        "id": "recharge", "icon": "📱",
        "label": "Recharge failed, money deducted",
        "text": "I can see the recharge did not go through even though the "
                "amount was debited. Let me check the refund for you now.",
        "keywords": ["recharge", "paise cut", "deducted", "prepaid"],
    },
    {
        "id": "refund", "icon": "💸",
        "label": "Refund still not received",
        "text": "Thank you for your patience. Let me look up exactly where "
                "your refund has reached and when it will land.",
        "keywords": ["refund", "money back", "reversal", "paise wapas"],
    },
    {
        "id": "delivery", "icon": "📦",
        "label": "Order has not arrived",
        "text": "I am sorry your order has not reached you. Let me check "
                "where the parcel is and what the courier has recorded.",
        "keywords": ["order", "delivery", "deliver", "parcel", "shipment"],
    },
    {
        "id": "network", "icon": "📶",
        "label": "Internet / network down",
        "text": "Sorry about the connection trouble. Let me check for an "
                "outage in your area before we try anything on your device.",
        "keywords": ["internet", "network", "signal", "slow", "connection"],
    },
    {
        "id": "account", "icon": "🔑",
        "label": "Cannot log in",
        "text": "Let me get you back into your account. I will check what is "
                "registered and send a fresh sign-in link.",
        "keywords": ["log in", "login", "password", "otp", "account"],
    },
    {
        "id": "escalate", "icon": "⚠️",
        "label": "Threatening to cancel",
        "text": "I understand, and I am sorry you have had to ask more than "
                "once. Let me take ownership of this and get it resolved today.",
        "keywords": ["cancel", "legal", "complaint", "consumer court", "escalate"],
    },
]





# How sure the knowledge base must be before we answer a customer without any
# agent involved. Deliberately higher than the 0.58 used merely to SHOW an
# article to an agent: putting an answer straight in front of a customer needs
# more confidence than putting one in front of a human who can overrule it.
# Measured matches ran 0.596-0.711, so 0.65 keeps only the strong half.


# How long we have to give the customer a FIRST reply, by how risky the
# conversation looks. An angry customer waiting fifteen minutes is a different
# problem from a calm one waiting four hours, so one flat target would be
# either far too tight or meaningless.
SLA_TARGET_MINUTES = {"high": 15, "medium": 60, "low": 240}
DEFAULT_SLA_MINUTES = 60          # when we have no risk reading yet


# Cases live in SQLite now. The JSON file it grew out of is kept as the
# migration source and is still what /api/export.json produces, so nothing
# that read the old format has been orphaned.
CASES_DB = os.path.join(HERE, "cases.db")
CASES_FILE = os.path.join(HERE, "cases.json")     # legacy, and export shape


# ==========================================================================
# Case store
# ==========================================================================
#
# One row per case: the fields we actually filter and sort on get real
# columns, and everything nested -- messages, facts, ratings, redactions --
# rides along as JSON in `data`.
#
# Fully normalising this would mean five more tables and a join for every
# read, and the app always wants the whole case anyway. This way the queries
# that matter can use an index, without pretending a support transcript is
# relational when it is not.

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    id              TEXT UNIQUE NOT NULL,
    status          TEXT,
    escalation_risk TEXT,
    sentiment       TEXT,
    opened_at       TEXT,
    closed_at       TEXT,
    data            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS cases_status ON cases(status);
CREATE INDEX IF NOT EXISTS cases_risk   ON cases(escalation_risk);
CREATE INDEX IF NOT EXISTS cases_opened ON cases(opened_at);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add a column to a table that already exists, so an existing database needs
# them applied by hand -- guarded, because ALTER TABLE has no IF NOT EXISTS.
MIGRATIONS = [
    ("owner", "ALTER TABLE cases ADD COLUMN owner TEXT"),
    # owner is the AGENT working a case. Until now nothing recorded who
    # RAISED it, which is the one thing "their own tickets" needs to know.
    ("customer", "ALTER TABLE cases ADD COLUMN customer TEXT"),
    ("subject", "ALTER TABLE cases ADD COLUMN subject TEXT"),
    ("category", "ALTER TABLE cases ADD COLUMN category TEXT"),
    ("updated_at", "ALTER TABLE cases ADD COLUMN updated_at TEXT"),
]


def apply_migrations(conn):
    have = {row["name"] for row in conn.execute("PRAGMA table_info(cases)")}
    for column, statement in MIGRATIONS:
        if column not in have:
            conn.execute(statement)

    # After the columns exist, not inside MIGRATIONS -- that list is pairs of
    # (column, ALTER), and an index is neither.
    conn.execute("CREATE INDEX IF NOT EXISTS cases_customer "
                 "ON cases(customer)")


def connect():
    """A fresh connection per call -- Flask serves requests on many threads,
    and a SQLite connection must not be shared across them."""
    conn = sqlite3.connect(CASES_DB, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


# auth.py and metering.py keep their tables in this same database, and the
# test suite repoints the path after import -- so hand them the factory, not
# the path.
auth.configure(connect)
actions.configure(connect)
pipeline.configure(
    now_iso=lambda: now_iso(),
    record_proposals=lambda sess: record_proposals(sess))
metering.configure(connect)

# Every Gemini call the engine makes now reports its token count here. The
# engine stays unaware of pricing, budgets and storage.
coach_core.USAGE_HOOK = metering.record


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.executescript(auth.SCHEMA)
        auth.apply_migrations(conn)
        conn.executescript(actions.SCHEMA)
        conn.executescript(metering.SCHEMA)
        apply_migrations(conn)


# lead is gone, folded into admin. An account still holding it would fail
# every guard and be unable to sign in anywhere, so it is moved rather than
# left stranded. priya moves too: the demo expects her to be the customer,
# and she owns no cases, so nothing follows her across.
RETIRED_ROLES = {"lead": "admin"}
DEMO_ROLE_FIXES = {"priya": "customer"}


def migrate_retired_roles():
    """Move accounts off roles that no longer exist. Returns what it moved."""
    moved = []
    for row in auth.list_users():
        username, role = row["username"], row["role"]
        wanted = RETIRED_ROLES.get(role) or (
            DEMO_ROLE_FIXES.get(username) if role != DEMO_ROLE_FIXES.get(username)
            else None)
        if not wanted or wanted == role:
            continue
        try:
            auth.set_role(username, wanted)
            moved.append((username, role, wanted))
        except ValueError:
            pass          # a role we cannot set is not worth failing boot over
    return moved


def backfill_resolution_modes():
    """Give older cases the resolution_mode they were closed under.

    Runs once at startup and is idempotent -- a row that already has the
    field is left alone, so this costs nothing on every boot after the first.
    Without it the dashboard would show a split bar covering only the cases
    saved since the feature shipped, which reads as "AI did nothing until
    last Tuesday" rather than as missing data.
    """
    filled = 0
    for case in load_cases():
        if case.get("resolution_mode") is not None:
            continue
        mode = resolution_mode_for(case)
        if mode is None:
            continue                  # nothing was ever sent on this one
        case["resolution_mode"] = mode
        save_case(case)
        filled += 1
    return filled


def migrate_from_json():
    """Import cases.json the first time, then never again.

    Idempotent: it only runs when the table is empty, so restarting the
    server cannot duplicate anything.
    """
    init_db()

    with connect() as conn:
        already = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    if already or not os.path.exists(CASES_FILE):
        return 0

    try:
        with open(CASES_FILE) as handle:
            cases = json.load(handle).get("cases", [])
    except (json.JSONDecodeError, OSError):
        return 0

    for case in cases:
        save_case(case)

    # Rename it once imported. Left in place it becomes a trap: delete the
    # database later and the migration would silently re-import a snapshot
    # that is now weeks out of date, quietly losing everything since.
    # Losing the rename is survivable -- the import already succeeded.
    with contextlib.suppress(OSError):
        os.replace(CASES_FILE, CASES_FILE + ".imported")

    return len(cases)


def _row_values(case):
    return (case.get("id"), case.get("status"), case.get("escalation_risk"),
            case.get("sentiment"), case.get("opened_at"), case.get("closed_at"),
            case.get("owner"), case.get("customer"), case.get("subject"),
            case.get("category"), case.get("updated_at"), json.dumps(case))


def save_case(case):
    """Insert or update ONE case.

    The JSON store had to rewrite every case on every message. This touches
    a single row, which is the whole reason for moving.
    """
    if not case.get("id"):
        return
    case["updated_at"] = now_iso()      # "Last updated", for the ticket list
    with connect() as conn:
        conn.execute("""
            INSERT INTO cases (id, status, escalation_risk, sentiment,
                               opened_at, closed_at, owner, customer,
                               subject, category, updated_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                escalation_risk=excluded.escalation_risk,
                sentiment=excluded.sentiment,
                opened_at=excluded.opened_at,
                closed_at=excluded.closed_at,
                owner=excluded.owner,
                customer=excluded.customer,
                subject=excluded.subject,
                category=excluded.category,
                updated_at=excluded.updated_at,
                data=excluded.data
        """, _row_values(case))


def load_cases():
    """Every saved case, oldest first -- the order the JSON list had."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT data FROM cases ORDER BY seq").fetchall()
        return [json.loads(row["data"]) for row in rows]
    except (sqlite3.Error, json.JSONDecodeError):
        # A broken store must not take the whole server down.
        return []


def save_cases(cases):
    """Replace the whole store. Same signature as the JSON version had, so
    anything written against the old API still works."""
    with connect() as conn:
        conn.executescript(SCHEMA)
        apply_migrations(conn)
        conn.execute("DELETE FROM cases")
        conn.executemany("""
            INSERT INTO cases (id, status, escalation_risk, sentiment,
                               opened_at, closed_at, owner, customer,
                               subject, category, updated_at, data)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [_row_values(c) for c in cases if c.get("id")])


def next_case_id(cases):
    """SC-1001, SC-1002, ... Readable, and stable across restarts."""
    numbers = []
    for case in cases:
        try:
            numbers.append(int(str(case.get("id", "")).split("-")[-1]))
        except ValueError:
            continue
    return f"SC-{(max(numbers) if numbers else 1000) + 1}"


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def signed_in_username():
    """The current user's name, or None outside a request.

    persist() runs inside a request in the app but is called directly by the
    test suite, where there is no request and no user to attribute a case to.
    """
    try:
        return current_user.username if current_user.is_authenticated else None
    except (RuntimeError, AttributeError):
        return None          # no request context -- tests, or a CLI command


RESOLUTION_MODES = ("ai_autonomous", "hybrid", "human")

# How close a sent reply has to be to the draft we offered before we call it
# the draft. Chosen by the brief; it is a ratio over the whole string, so an
# agent who keeps the shape and swaps a sentence still lands above it, while
# somebody who types their own answer does not.
HYBRID_SIMILARITY = 0.60


def similarity(left, right):
    """0.0-1.0 on two pieces of text, case and whitespace insensitive."""
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, " ".join(str(left).lower().split()),
                           " ".join(str(right).lower().split())).ratio()


def outgoing_sources(case):
    """Who composed each outgoing message on this case.

    Messages written from now on carry `source` on them, set at the moment
    they were sent, which is the only time anybody actually knows. Older ones
    predate the field, so it is INFERRED here by the same rules the live path
    applies -- a reply that closely matches the draft we offered was worked
    from that draft. That is what makes the backfill and the live value the
    same number rather than two different definitions wearing one name.
    """
    auto = (case.get("auto_reply") or {}).get("reply", "")
    suggestion = case.get("suggestion") or ""

    sources = []
    for message in case.get("messages", []):
        if message.get("speaker") != "agent":
            continue                      # only outgoing messages have an author

        recorded = message.get("source")
        if recorded in ("ai", "hybrid", "human"):
            sources.append(recorded)
            continue

        text = message.get("text", "")
        if auto and similarity(text, auto) > 0.98:
            sources.append("ai")          # this is the deflection itself
        elif similarity(text, suggestion) > HYBRID_SIMILARITY:
            sources.append("hybrid")
        else:
            sources.append("human")

    return sources


def resolution_mode_for(case):
    """Which of the three modes closed this case, or None if nothing was sent.

    A case nobody has replied to yet has no resolver, and guessing one would
    put a case in the split bar that nobody has worked.
    """
    sources = outgoing_sources(case)
    if not sources:
        return None

    if all(s == "ai" for s in sources):
        return "ai_autonomous"
    if all(s == "human" for s in sources):
        return "human"
    # Everything else had a person AND the coach in it: a used draft, or a
    # deflection an agent later followed up by hand. Neither "AI end to end"
    # nor "a human wrote every word" is true of those, and calling them
    # either one would overstate exactly the number this exists to report.
    return "hybrid"


CHANNELS = ("typed", "voice")


def read_channel(body):
    """How the caller says this message was entered. Anything unrecognised is
    treated as typed -- the transcript should never claim a message was
    dictated on the strength of a value we do not know."""
    wanted = (body or {}).get("channel", "typed")
    return wanted if wanted in CHANNELS else "typed"


def may_use_write_actions():
    """Whether this session may arm, or approve, a write action.

    A rank floor, so a customer -- who sits below every staff role -- can
    never reach it whatever the checkbox in their browser says. The check
    lives here rather than being repeated at each call site, because a write
    action guarded in three places out of four is not guarded.
    """
    return (current_user.is_authenticated
            and current_user.at_least("admin"))


def record_proposals(session):
    """Write every write the model just asked for into the audit trail.

    Called on the customer turn, before the agent is shown anything. An
    identical proposal already waiting on this case is not duplicated -- the
    model re-asks for the same refund on every turn of a conversation that
    needs one, and a queue of eleven identical cards is a way of making sure
    nobody reads any of them.
    """
    if not session.case_id:
        return

    waiting = actions.pending_for_case(session.case_id)
    known = {(p["action"], json.dumps(p["args"], sort_keys=True))
             for p in waiting}

    for fact in session.facts or []:
        if not fact.get("proposed"):
            continue
        key = (fact["name"], json.dumps(fact.get("args") or {}, sort_keys=True))
        if key in known:
            continue
        known.add(key)
        actions.propose(session.case_id, signed_in_username(),
                        fact["name"], fact.get("args") or {},
                        reason=session.state.key_issue or None)


def may_see_case(case):
    """Whether the signed-in user is allowed to open this one case.

    Leads and admins see everything -- that is what the dashboard is. An agent
    sees their own, plus anything still unowned, which they claim by opening.
    """
    if not current_user.is_authenticated:
        return False
    if current_user.at_least("admin"):
        return True
    owner = case.get("owner")
    return owner is None or owner == current_user.username


# ==========================================================================
# The conversation currently open
# ==========================================================================
class LiveSession:
    """The one conversation an agent is working on right now.

    It is also a case: as soon as it has a message it is written to
    cases.json with status "pending", so the dashboard can count it.
    """

    def __init__(self):
        self.reset()

    def reset(self):
        self.case_id = None          # assigned on the first message
        self.owner = None            # the agent it belongs to
        # Who RAISED it, and how they described it. Set by the portal; the
        # console leaves them empty because an agent-side case has no
        # customer account behind it.
        self.customer = None
        self.subject = ""
        self.category = ""
        self.opened_at = None
        self.state = coach_core.ConversationState()
        self.last_customer_message = ""
        self.trajectory = []
        self.calls = 0
        self.last_feedback = None
        self.last_suggestion = ""
        self.last_latency_ms = 0
        self.unanswered = []         # questions nothing in our docs covers
        self.facts = []              # back-office lookups for this turn
        self.allow_writes = False    # actions stay gated until a person says so
        self.auto_reply = None       # set when we answered without an agent
        self.first_response_at = None  # when the customer first heard back
        self.redactions = []         # personal details kept from the model
        self.ratings = []            # thumbs on each suggestion we produced

    _coach = None

    @property
    def coach(self):
        if self._coach is None:
            self._coach = coach_core.AICoach()
        return self._coach

    # -- persistence ------------------------------------------------------
    def real_calls(self):
        """How many model calls this case has actually made.

        Read from the metering table instead of counted by hand. Every call
        goes through _call_model or report_usage, both of which record a row,
        so this cannot miss the tool-calling loop or the embeddings the way
        the manual counter did -- it was showing 2 for a turn that made 7.

        A case saved before metering existed has no rows; that falls back to
        whatever it stored rather than rewriting its history to zero.
        """
        if not self.case_id:
            return self.calls
        try:
            counted = metering.for_case(self.case_id)["calls"]
        except Exception:
            return self.calls          # metering must never break a turn
        return counted or self.calls

    def as_case(self, status="pending", closed_at=None):
        case = {
            "id": self.case_id,
            "owner": self.owner,
            "customer": self.customer,
            "subject": self.subject,
            "category": self.category,
            "opened_at": self.opened_at,
            "first_response_at": self.first_response_at,
            "closed_at": closed_at,
            "status": status,
            "turns": len(self.state.history),
            "calls": self.real_calls(),
            "sentiment": self.state.sentiment,
            "urgency": self.state.urgency,
            "escalation_risk": self.state.escalation_risk,
            "frustration": self.state.frustration,
            "trend": self.state.trend,
            "key_issue": self.state.key_issue,
            "intent": self.state.intent,
            "intent_confidence": self.state.intent_confidence,
            "emotion": self.state.emotion,
            "trajectory": list(self.trajectory),
            # The detail view shows these, so they have to be saved with the
            # case -- previously they lived only in memory and were lost the
            # moment the conversation was closed.
            "feedback": self.last_feedback,
            "suggestion": self.last_suggestion,
            "unanswered": list(self.unanswered),
            "redactions": list(self.redactions),
            "ratings": list(self.ratings),
            "facts": list(self.facts),
            "auto_reply": self.auto_reply,
            "messages": [
                {"speaker": m.speaker, "text": m.text,
                 "source": getattr(m, "source", "human"),
                 "channel": getattr(m, "channel", "typed"),
                 "author": getattr(m, "author", "")}
                for m in self.state.history
            ],
        }
        # Derived, never set by hand, so it cannot drift from the messages it
        # describes. Computed on every save rather than once at close: a case
        # that reopens and gets a human reply has genuinely changed hands.
        case["resolution_mode"] = resolution_mode_for(case)
        return case

    def persist(self, status="pending", closed_at=None, reopen=False):
        """Insert or update this conversation in cases.json.

        reopen=True is passed only when a CUSTOMER has just written. That is
        the one situation where an auto-resolved case should fall back to
        pending -- saving the case for any other reason (a reset, an agent
        reply) must not quietly undo a deflection.
        """
        cases = load_cases()

        if self.case_id is None:
            self.case_id = next_case_id(cases)
            self.opened_at = now_iso()
            if self.owner is None:
                self.owner = signed_in_username()

        # Whatever this request spends from here on belongs to this case.
        metering.bill_to(self.case_id)

        record = self.as_case(status=status, closed_at=closed_at)

        for index, case in enumerate(cases):
            if case.get("id") == self.case_id:
                # Never downgrade a resolved case back to pending.
                # A case a person closed stays closed, always.
                was = case.get("status")
                keep_closed = (was == "resolved")

                # An auto-resolved case stays closed too -- UNLESS the customer
                # has just written again, which means the automatic answer did
                # not land. Deflection you have to follow up is not deflection.
                if was == "auto_resolved" and not reopen:
                    keep_closed = True

                if keep_closed and status == "pending":
                    record["status"] = was
                    record["closed_at"] = case.get("closed_at")

                # The customer owns these, not the console. as_case() rebuilds
                # the row from session state, which has never seen a rating --
                # so without this an agent reopening a rated case and typing
                # one line would silently delete the rating.
                for field in CUSTOMER_FIELDS:
                    if field in case:
                        record[field] = case[field]

                cases[index] = record
                break
        else:
            cases.append(record)

        save_case(record)          # one row, not the whole store
        return record

    def load(self, case):
        """Pull a saved case back into the live session so it can continue.

        The console only ever holds ONE conversation, so opening a case from
        the work queue means rehydrating it here -- transcript, analysis,
        trajectory and all -- rather than starting something new.
        """
        self.reset()

        self.case_id = case.get("id")
        metering.bill_to(self.case_id)
        # An unowned case -- one from before accounts existed -- is claimed by
        # whoever opens it. Otherwise those cases would belong to nobody and
        # no agent could ever pick them up again.
        self.owner = case.get("owner") or signed_in_username()
        self.customer = case.get("customer")
        self.subject = case.get("subject") or ""
        self.category = case.get("category") or ""
        self.opened_at = case.get("opened_at")
        self.first_response_at = case.get("first_response_at")
        self.trajectory = list(case.get("trajectory") or [])
        self.calls = case.get("calls", 0)
        self.unanswered = list(case.get("unanswered") or [])
        self.redactions = list(case.get("redactions") or [])
        self.ratings = list(case.get("ratings") or [])
        self.last_feedback = case.get("feedback")
        self.last_suggestion = case.get("suggestion") or ""

        for message in case.get("messages", []):
            self.state.add_message(message.get("speaker", "customer"),
                                   message.get("text", ""),
                                   message.get("source", "human"),
                                   message.get("channel", "typed"),
                                   message.get("author", ""))

        # The agent replies to the last thing the CUSTOMER said, which is not
        # necessarily the last line of the transcript.
        for message in reversed(case.get("messages", [])):
            if message.get("speaker") == "customer":
                self.last_customer_message = message.get("text", "")
                break

        self.state.sentiment = case.get("sentiment", "unknown")
        self.state.urgency = case.get("urgency", "unknown")
        self.state.escalation_risk = case.get("escalation_risk", "unknown")
        self.state.frustration = case.get("frustration", 0)
        self.state.trend = case.get("trend", "unknown")
        self.state.key_issue = case.get("key_issue", "")
        # Cases saved before intent extraction have none of these, so they
        # fall back to empty and the panel shows a dash rather than stale
        # readings from whatever was open previously.
        self.state.intent = case.get("intent", "")
        self.state.intent_confidence = case.get("intent_confidence")
        self.state.emotion = case.get("emotion", "")

    def as_dict(self):
        return {
            "case_id": self.case_id,
            "history": [
                {"speaker": m.speaker, "text": m.text,
                 "channel": getattr(m, "channel", "typed"),
                 "source": getattr(m, "source", "human")}
                for m in self.state.history
            ],
            "sentiment": self.state.sentiment,
            "urgency": self.state.urgency,
            "escalation_risk": self.state.escalation_risk,
            "frustration": self.state.frustration,
            "trend": self.state.trend,
            "key_issue": self.state.key_issue,
            "intent": self.state.intent,
            "intent_confidence": self.state.intent_confidence,
            "emotion": self.state.emotion,
            "trajectory": self.trajectory,
            "calls": self.real_calls(),
            "model": self._coach.model if self._coach else None,
            "last_feedback": self.last_feedback,
            "last_suggestion": self.last_suggestion,
            "last_latency_ms": self.last_latency_ms,
            "facts": self.facts,
            "redactions": self.redactions,
            "ratings": self.ratings,
            "allow_writes": self.allow_writes,
            "auto_reply": self.auto_reply,
            # Proposals and completed runs for this case. Read from the audit
            # table rather than kept on the session, so reopening a case from
            # the dashboard shows the same history the trail does.
            "actions": actions.for_case(self.case_id) if self.case_id else [],
        }


session = LiveSession()


def parse_time(value):
    """Read one of our ISO timestamps back, or None if it is missing/broken."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def sla_for(case, now=None):
    """Work out where one case stands against its first-response target.

    Statuses:
      met       -- the customer got a reply inside the target
      breached  -- they did not, or are still waiting past it
      at_risk   -- still waiting, and more than half the target is gone
      on_track  -- still waiting, comfortably inside the target
      unknown   -- saved before we started recording response times
    """
    now = now or datetime.now(UTC)

    minutes = SLA_TARGET_MINUTES.get(case.get("escalation_risk"),
                                     DEFAULT_SLA_MINUTES)
    target = minutes * 60

    opened = parse_time(case.get("opened_at"))
    responded = parse_time(case.get("first_response_at"))
    closed = parse_time(case.get("closed_at"))
    is_closed = case.get("status") in ("resolved", "auto_resolved")

    result = {"target_minutes": minutes, "first_response_seconds": None,
              "resolution_seconds": None, "elapsed_seconds": None,
              "remaining_seconds": None, "status": "unknown"}

    if opened is None:
        return result

    if closed is not None:
        result["resolution_seconds"] = int((closed - opened).total_seconds())

    if responded is not None:
        taken = int((responded - opened).total_seconds())
        result["first_response_seconds"] = taken
        result["elapsed_seconds"] = taken
        result["status"] = "met" if taken <= target else "breached"
        result["remaining_seconds"] = target - taken
        return result

    if is_closed:
        # Closed without us knowing when the reply went out -- these are
        # cases saved before response times were recorded. Say so rather
        # than inventing a number.
        return result

    # Still waiting for a first reply: the clock is running.
    waited = int((now - opened).total_seconds())
    result["elapsed_seconds"] = waited
    result["remaining_seconds"] = target - waited

    if waited > target:
        result["status"] = "breached"
    elif waited > target / 2:
        result["status"] = "at_risk"
    else:
        result["status"] = "on_track"

    return result


# ==========================================================================
# Customer satisfaction
# ==========================================================================
# A rating stays editable for a day. Long enough that somebody who rated in
# irritation and then got a good outcome can correct it; short enough that the
# history stops moving under the dashboard.
CSAT_EDIT_HOURS = 24

# Written against the stored row by the rating endpoint, never by the live
# session. persist() copies them forward; see the note there.
CUSTOMER_FIELDS = ("csat_score", "csat_comment", "csat_at")

# Below this many ratings an average is reported WITH its sample size and
# rendered faintly. Ten is not a significance threshold -- it is the point
# below which a single rating moves the mean by more than a tenth of a star,
# which is the resolution the tile is displayed at.
CSAT_SMALL_SAMPLE = 10

CLOSED = ("resolved", "auto_resolved")


def record_csat(case, score, comment="", now=None):
    """Attach a customer rating to a case.

    Returns (case, None) or (None, why). The rules live here rather than in
    the route so the portal, the chat view and the test suite are all held to
    the same ones.
    """
    now = now or datetime.now(UTC)

    if case.get("status") not in CLOSED:
        return None, "That case is not resolved yet."

    # Parsed through str() so a float like 2.5 raises rather than truncating
    # to 2. Half a star is not a rating this scale offers, and quietly
    # rounding it records something the customer did not say.
    if isinstance(score, bool):
        return None, "A rating must be a whole number of stars."
    try:
        value = int(str(score).strip())
    except (TypeError, ValueError):
        return None, "A rating must be a whole number of stars."
    if not 1 <= value <= 5:
        return None, "A rating must be between 1 and 5 stars."

    # csat_at anchors the edit window and is NOT moved by a later edit --
    # otherwise each change would buy another day and the window would never
    # close.
    first = parse_time(case.get("csat_at"))
    if first is not None:
        hours = (now - first).total_seconds() / 3600
        if hours > CSAT_EDIT_HOURS:
            return None, (f"This rating can no longer be changed. Ratings stay "
                          f"open for {CSAT_EDIT_HOURS} hours.")
    else:
        case["csat_at"] = now.isoformat(timespec="seconds")

    case["csat_score"] = value
    case["csat_comment"] = (comment or "").strip()[:280]
    return case, None


def rated(cases):
    return [c for c in cases if isinstance(c.get("csat_score"), int)]


def csat_summary(cases):
    """The CSAT tile: an average, and how much of the closed work it covers."""
    closed = [c for c in cases if c.get("status") in CLOSED]
    scores = [c["csat_score"] for c in rated(closed)]

    return {
        "average": round(sum(scores) / len(scores), 2) if scores else None,
        "count": len(scores),
        "eligible": len(closed),
        "response_rate": round(100 * len(scores) / len(closed)) if closed else 0,
        "small_sample": len(scores) < CSAT_SMALL_SAMPLE,
    }


def csat_by_mode(cases):
    """Average CSAT per resolution mode -- the comparison worth having.

    Each mode carries its own sample size, because "AI 4.8 vs human 4.1" means
    nothing until you know whether that is 40 ratings or two.
    """
    out = []
    for mode in RESOLUTION_MODES:
        scores = [c["csat_score"] for c in rated(cases)
                  if (c.get("resolution_mode") or resolution_mode_for(c)) == mode]
        out.append({
            "mode": mode,
            "average": round(sum(scores) / len(scores), 2) if scores else None,
            "count": len(scores),
            "small_sample": len(scores) < CSAT_SMALL_SAMPLE,
        })
    return out


def median_first_reply(cases):
    """Median seconds from the customer's first message to the first reply.

    The median, not the mean: one case that sat overnight drags a mean far
    enough to make a good week look bad, and the tile is meant to describe
    the typical wait rather than the worst one.
    """
    waits = sorted(
        seconds for seconds in
        (sla_for(case).get("first_response_seconds") for case in cases)
        if isinstance(seconds, int) and seconds >= 0
    )
    if not waits:
        return {"median_seconds": None, "count": 0}

    middle = len(waits) // 2
    median = (waits[middle] if len(waits) % 2
              else (waits[middle - 1] + waits[middle]) / 2)
    return {"median_seconds": int(median), "count": len(waits)}


# The knowledge base's topics are the issue groups this app had before intents
# existed. Mapping them onto the intent vocabulary means one category axis
# rather than two that nearly agree.
TOPIC_AS_INTENT = {
    "refund": "Refund status",
    "recharge": "Recharge failed",
    "delivery": "Order & delivery",
    "network": "Network issue",
    "account": "Account & login",
}
UNCATEGORISED = "Uncategorised"


def category_of(case):
    """Which bucket a case belongs in, best source first."""
    intent = case.get("intent")
    if intent:
        return intent

    # Older cases have no intent, but a deflection or a rated suggestion
    # recorded which help article matched, which is the same question asked
    # a different way.
    topic = (case.get("auto_reply") or {}).get("topic")
    if not topic:
        topic = next((r.get("topic") for r in reversed(case.get("ratings") or [])
                      if r.get("topic")), None)
    return TOPIC_AS_INTENT.get(topic, UNCATEGORISED)


def category_volume(cases):
    """Ticket count per category, biggest first."""
    counts = {}
    for case in cases:
        counts[category_of(case)] = counts.get(category_of(case), 0) + 1

    total = sum(counts.values())
    rows = [{"category": name, "count": n,
             "share": round(100 * n / total) if total else 0}
            for name, n in counts.items()]
    # Biggest first, and Uncategorised last whatever its size -- it is the
    # absence of an answer, not one of the answers.
    rows.sort(key=lambda r: (r["category"] == UNCATEGORISED, -r["count"],
                             r["category"]))
    return {"rows": rows, "total": total}











@app.before_request
def open_billing_account():
    """Attribute anything this request spends to whoever is making it.

    Done here rather than at each call site so a route added later is metered
    by default instead of by remembering to.
    """
    metering.begin(signed_in_username())


def failure(error, status=502):
    return jsonify({"ok": False, "error": str(error)}), status


def over_budget():
    """429 with a reason, or None to carry on.

    Checked once at the top of a turn rather than before each model call:
    stopping half way through a tool-calling loop would leave the customer
    with a reply quoting a lookup that never finished.
    """
    ok, why = metering.check_limits(signed_in_username())
    if ok:
        return None

    response = jsonify({"ok": False, "error": why["error"],
                        "limit": why["limit"]})
    response.status_code = 429
    response.headers["Retry-After"] = str(why["retry_after"])
    return response


# ==========================================================================
# Sign in / sign out
# ==========================================================================
@app.get("/login")
def login_page():
    if current_user.is_authenticated:
        return redirect("/")
    return send_from_directory(app.static_folder, "login.html")


# The role a page needs, so the login can tell whether sending somebody back
# where they came from would only bounce them again.
PAGE_ROLES = {"/dashboard": "admin", "/": "agent", "/portal": "customer"}


def landing_for(user, wanted=""):
    """Where to send someone once they have signed in.

    They get `wanted` only if it is a path on this site AND their role can
    actually open it. An agent who followed a dashboard link while signed out
    would otherwise log in and land straight on "not allowed", which reads as
    the login having failed.
    """
    home = user.landing

    wanted = (wanted or "").strip()
    # A protocol-relative "//evil.example" is a path to a browser and an open
    # redirect to everyone else.
    if not wanted.startswith("/") or wanted.startswith("//"):
        return home

    needed = next((role for path, role in PAGE_ROLES.items()
                   if wanted == path or wanted.startswith(path.rstrip("/") + "/")),
                  None)
    if needed is None:
        return wanted
    # The portal is audience, not seniority: an admin outranks a customer but
    # still does not belong in the customer's workspace.
    allowed = (user.role == "customer" if needed == "customer"
               else user.at_least(needed))
    return wanted if allowed else home


@app.post("/api/login")
def do_login():
    body = request.json or {}

    # "email" is what the form sends; "username" is what --add-user makes and
    # what the test suite uses. find_user() accepts either.
    handle = body.get("email") or body.get("username", "")
    user, why = auth.authenticate(handle, body.get("password", ""))
    if user is None:
        # 401 with a deliberately vague reason -- see auth.authenticate().
        return jsonify({"ok": False, "error": why}), 401

    # The role card is a claim about which workspace you meant to open. It is
    # checked against the account rather than trusted -- picking Admin does
    # not make you one -- so a mismatch is a clear message instead of a
    # confusing landing somewhere you did not expect.
    claimed = body.get("role")
    if claimed and claimed in auth.ROLES and claimed != user.role:
        return jsonify({"ok": False, "error": (
            f"That is a {user.role} account. Choose the "
            f"{user.role.title()} card to sign in.")}), 403

    login_user(user, remember=False, duration=None)
    return jsonify({"ok": True, "user": user.as_dict(),
                    "next": landing_for(user, body.get("next", ""))})


# Self-service sign-up creates a CUSTOMER. Somebody who finds the login page
# and makes an account is a person with a problem, not a member of staff --
# and this is what keeps the console, the dashboard, the write-action gate
# and the exports behind an account that somebody made deliberately with
# --add-user.
SIGNUP_ROLE = "customer"

# Letters, digits, dot, dash, underscore. create_user() lowercases and
# strips, but it would otherwise accept a username with spaces in it, which
# then reads badly everywhere it is displayed.
USERNAME_SHAPE = re.compile(r"[a-z0-9._-]{3,32}")


@app.post("/api/register")
def register():
    """Create an account from the login page, and sign it straight in.

    Deliberately NOT role-selectable. The form cannot ask for a role and the
    server does not read one if it is sent.
    """
    body = request.json or {}
    username = (body.get("username") or "").strip().lower()
    password = body.get("password") or ""
    confirm = body.get("confirm") or ""
    display = (body.get("display_name") or "").strip()

    if not USERNAME_SHAPE.fullmatch(username):
        return jsonify({"ok": False, "error": (
            "A username is 3-32 characters, using letters, numbers, dot, "
            "dash or underscore.")}), 400

    if password != confirm:
        return jsonify({"ok": False,
                        "error": "The two passwords do not match."}), 400

    try:
        auth.create_user(username, password, SIGNUP_ROLE,
                         display_name=display or None)
    except ValueError as why:
        # create_user already refuses a short password and a duplicate name.
        return jsonify({"ok": False, "error": str(why)}), 400

    user, problem = auth.authenticate(username, password)
    if user is None:
        return jsonify({"ok": False, "error": problem}), 400

    login_user(user, remember=False, duration=None)
    return jsonify({"ok": True, "created": True, "user": user.as_dict(),
                    "next": landing_for(user, body.get("next", ""))})


@app.post("/api/logout")
def do_logout():
    logout_user()
    return jsonify({"ok": True})


@app.get("/api/me")
def me():
    """Who is signed in. The front ends call this first and hide whatever the
    role cannot reach, so nobody is shown a button that will only 403."""
    if not current_user.is_authenticated:
        return jsonify({"ok": False, "auth": "required"}), 401
    return jsonify({"ok": True, "user": current_user.as_dict()})


# ==========================================================================
# The customer's workspace
# ==========================================================================
# What a customer is allowed to know about their own ticket. THREE states,
# and this is the only function that decides which -- so when the decision
# engine lands and adds an "escalated" status, it joins the amber row here
# and nowhere else.
CUSTOMER_STATES = {
    "ai_handled":      {"label": "Handled by AI", "tone": "green"},
    "human_reviewing": {"label": "A human agent is reviewing", "tone": "amber"},
    "resolved":        {"label": "Resolved", "tone": "grey"},
}

CATEGORIES = ["Recharge", "Refund", "Order & Delivery", "Network",
              "Account & Login", "Other"]


def customer_status_of(case):
    """Which of the three states the customer sees. Nothing else leaks."""
    status = case.get("status")
    if status == "auto_resolved":
        return "ai_handled"
    if status == "resolved":
        return "resolved"
    return "human_reviewing"        # pending, escalated, anything open


def as_customer_case(case, *, with_messages=False):
    """The ONLY shape a case is ever sent to a customer in.

    An allow-list, naming every field it emits, rather than a deny-list that
    strips the forbidden ones. A deny-list leaks every field anybody adds
    later; this fails closed. Nothing about sentiment, urgency, escalation
    risk, gates, scores, coaching, lookups or cost can travel through here,
    because none of them is named.
    """
    state = customer_status_of(case)
    out = {
        "id": case.get("id"),
        "subject": case.get("subject") or (case.get("key_issue") or "Support request"),
        "category": case.get("category") or "Other",
        "status": state,
        "status_label": CUSTOMER_STATES[state]["label"],
        "status_tone": CUSTOMER_STATES[state]["tone"],
        "updated_at": case.get("updated_at") or case.get("opened_at"),
        "opened_at": case.get("opened_at"),
        # Their own rating and their own words. Safe to return for the same
        # reason their own messages are: they wrote it.
        "rating": case.get("csat_score"),
        "rating_comment": case.get("csat_comment") or "",
        "can_rate": case.get("status") in CLOSED,
        "rating_editable": rating_still_open(case),
    }

    if with_messages:
        out["messages"] = [
            {
                "speaker": m.get("speaker"),
                "text": m.get("text", ""),
                # Who the customer sees it from. An agent's own name when a
                # person took over, so the handover is visible to them.
                "from": ("You" if m.get("speaker") == "customer"
                         else m.get("author")
                         or ("AI assistant" if m.get("source") == "ai"
                             else "Support agent")),
                "is_ai": m.get("speaker") != "customer" and m.get("source") == "ai",
            }
            for m in case.get("messages", [])
        ]
    return out


def rating_still_open(case):
    """Whether this rating can still be given or changed.

    A closed case with no rating is open for one; a rated one stays editable
    for CSAT_EDIT_HOURS from the FIRST rating, which is the same window
    record_csat() enforces -- this only tells the page what to draw.
    """
    if case.get("status") not in CLOSED:
        return False
    first = parse_time(case.get("csat_at"))
    if first is None:
        return True
    return (datetime.now(UTC) - first).total_seconds() <= CSAT_EDIT_HOURS * 3600


def cases_for_customer(username):
    """Their own tickets, newest activity first."""
    mine = [c for c in load_cases() if c.get("customer") == username]
    mine.sort(key=lambda c: c.get("updated_at") or c.get("opened_at") or "",
              reverse=True)
    return mine


def customer_case_or_none(case_id, username):
    """One case, only if it belongs to this customer.

    Callers answer a miss with 404 rather than 403: "that case exists but is
    not yours" is itself a small leak.
    """
    for case in load_cases():
        if case.get("id") == case_id and case.get("customer") == username:
            return case
    return None


def scoped_session_for(case):
    """A short-lived session holding ONE case.

    The console's module-level session is a single conversation for the whole
    process. A customer typing at the same time as an agent would land in the
    agent's transcript. load() + persist() already bracket every turn, so a
    per-request session is a legitimate unit of work.
    """
    sess = LiveSession()
    sess.load(case)
    return sess


@app.get("/portal")
@require_exact("customer")
def portal():
    """The customer's ticket list."""
    return send_from_directory(app.static_folder, "portal.html")


@app.get("/portal/chat/<case_id>")
@require_exact("customer")
def portal_chat(case_id):
    """One conversation. Ownership is checked by the API the page calls."""
    if customer_case_or_none(case_id, signed_in_username()) is None:
        return redirect("/portal?error=not-your-ticket")
    return send_from_directory(app.static_folder, "portal-chat.html")


@app.get("/api/portal/tickets")
@require_exact("customer")
def portal_tickets():
    me = signed_in_username()
    return jsonify({"ok": True, "categories": CATEGORIES,
                    "tickets": [as_customer_case(c) for c in cases_for_customer(me)]})


@app.post("/api/portal/tickets")
@require_exact("customer")
def portal_new_ticket():
    """Raise a ticket, then run the same pipeline the console runs."""
    body = request.json or {}
    subject = (body.get("subject") or "").strip()
    category = (body.get("category") or "").strip()
    description = (body.get("description") or "").strip()

    if not subject:
        return jsonify({"ok": False, "error": "A subject is required."}), 400
    if not description:
        return jsonify({"ok": False, "error": "Tell us what happened."}), 400
    if category not in CATEGORIES:
        category = "Other"

    refused = over_budget()
    if refused:
        return refused

    me = signed_in_username()
    sess = LiveSession()
    sess.customer = me
    sess.subject = subject[:120]
    sess.category = category

    try:
        pipeline.run_customer_turn(sess, description)
    except Exception as error:
        return failure(error)

    case = next((c for c in load_cases() if c.get("id") == sess.case_id), None)
    return jsonify({"ok": True, "ticket": as_customer_case(case or {})})


@app.get("/api/portal/cases/<case_id>")
@require_exact("customer")
def portal_case(case_id):
    case = customer_case_or_none(case_id, signed_in_username())
    if case is None:
        return jsonify({"ok": False, "error": f"No ticket {case_id}."}), 404
    return jsonify({"ok": True, "ticket": as_customer_case(case, with_messages=True)})


@app.post("/api/portal/cases/<case_id>/rating")
@require_exact("customer")
def portal_rate(case_id):
    """The customer rates their own ticket.

    The agent-side /api/cases/<id>/csat cannot serve this: it is guarded by
    require_role("agent"), and a customer sits below that on purpose. Rather
    than widen that guard, this route answers "is it yours" the way the rest
    of the portal does -- and both call the SAME record_csat(), so the rules
    about stars, comments and the 24-hour window have one implementation.
    """
    case = customer_case_or_none(case_id, signed_in_username())
    if case is None:
        return jsonify({"ok": False, "error": f"No ticket {case_id}."}), 404

    body = request.json or {}
    updated, why = record_csat(case, body.get("score"), body.get("comment", ""))
    if updated is None:
        return jsonify({"ok": False, "error": why}), 400

    save_case(updated)
    return jsonify({"ok": True,
                    "ticket": as_customer_case(updated, with_messages=True)})


@app.post("/api/portal/cases/<case_id>/message")
@require_exact("customer")
def portal_message(case_id):
    """Carry on the conversation. Same pipeline, scoped session."""
    text = ((request.json or {}).get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    case = customer_case_or_none(case_id, signed_in_username())
    if case is None:
        return jsonify({"ok": False, "error": f"No ticket {case_id}."}), 404

    refused = over_budget()
    if refused:
        return refused

    sess = scoped_session_for(case)
    try:
        pipeline.run_customer_turn(sess, text)
    except Exception as error:
        return failure(error)

    fresh = customer_case_or_none(case_id, signed_in_username())
    return jsonify({"ok": True,
                    "ticket": as_customer_case(fresh or {}, with_messages=True)})


# ==========================================================================
# Pages
# ==========================================================================
@app.get("/")
@require_role("agent")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/dashboard")
@require_role("admin")
def dashboard():
    return send_from_directory(app.static_folder, "dashboard.html")


# ==========================================================================
# API -- the live conversation
# ==========================================================================
@app.get("/api/health")
def health():
    """Liveness, for a load balancer or a container probe.

    Deliberately NOT behind a role -- a probe cannot sign in. So an anonymous
    caller gets only "the process is up", and the key path and model name,
    which are configuration, are shown to a signed-in user only.
    """
    if not current_user.is_authenticated:
        return jsonify({"ok": True})

    key_file = coach_core.find_key_file()
    has_key = bool(coach_core.get_gemini_api_key(prompt_if_missing=False))
    return jsonify({
        "ok": True,
        "has_key": has_key,
        "key_file": key_file or None,
        "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite"),
    })


@app.post("/api/customer")
@require_role("agent")
def customer_message():
    """A customer message, from the agent console.

    The turn itself lives in pipeline.run_customer_turn() so that the portal
    runs exactly the same one. All this route does is HTTP.
    """
    body = request.json or {}
    text = (body.get("text", "") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    refused = over_budget()
    if refused:
        return refused

    try:
        turn = pipeline.run_customer_turn(
            session, text, channel=read_channel(body))
    except Exception as error:
        return failure(error)

    return jsonify({
        "ok": True,
        "analysis": turn.analysis,
        "latency_ms": turn.latency_ms,
        "model_used": getattr(session.coach, "last_model_used",
                              session.coach.model),
        "facts": turn.facts,
        "auto_reply": turn.auto_reply,
        "suggestion": turn.suggestion,
        "state": session.as_dict(),
    })


@app.post("/api/agent")
@require_role("agent")
def agent_message():
    body = request.json or {}
    text = (body.get("text", "") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "Empty message."}), 400

    if not session.last_customer_message:
        return jsonify({
            "ok": False,
            "error": "There is no customer message to score this against yet.",
        }), 400

    refused = over_budget()
    if refused:
        return refused

    # Two ways to land on hybrid. The console says so outright when the agent
    # pressed "Use this reply"; the similarity check catches the agent who
    # copied the draft by hand, or pressed the button, edited a line and sent
    # it. Either is a draft that did work, which is what hybrid means.
    clicked = bool(body.get("used_suggestion"))
    close = similarity(text, session.last_suggestion) > HYBRID_SIMILARITY
    # The agent's name travels with the message: the customer's chat shows
    # it, so a person taking over from the AI is visible to them.
    who = getattr(current_user, "display_name", None) or signed_in_username()
    session.state.add_message(
        "agent", text, source="hybrid" if (clicked or close) else "human",
        channel=read_channel(body), author=who or "")

    # The moment the customer first hears back stops the SLA clock.
    if session.first_response_at is None:
        session.first_response_at = now_iso()

    started = time.perf_counter()
    try:
        feedback = session.coach.evaluate_agent_response(
            session.last_customer_message, text
        )
        # Hand over the analysis we already computed on the customer's turn,
        # so the draft is written for THIS customer's mood -- and let
        # suggest_reply quote the matching help article.
        suggestion = session.coach.suggest_reply(
            session.last_customer_message,
            session.state.history,
            analysis={
                "sentiment": session.state.sentiment,
                "urgency": session.state.urgency,
                "key_issue": session.state.key_issue,
            },
            facts=session.facts,
        )
    except Exception as error:
        session.persist()
        return failure(error)
    note_redactions(session)
    elapsed = int((time.perf_counter() - started) * 1000)

    session.last_feedback = {
        "tone_score": feedback.tone_score,
        "empathy_score": feedback.empathy_score,
        "clarity_score": feedback.clarity_score,
        "coaching_tip": feedback.coaching_tip,
    }
    session.last_suggestion = suggestion
    session.last_latency_ms = elapsed

    article = getattr(session.coach, "last_article", None)
    open_rating_slot(session, article is not None,
                     article.get("topic") if article else None)
    session.persist()

    return jsonify({
        "ok": True,
        "feedback": session.last_feedback,
        "suggestion": suggestion,
        "latency_ms": elapsed,
        "model_used": getattr(session.coach, "last_model_used", session.coach.model),
        "state": session.as_dict(),
    })


@app.post("/api/allow-writes")
@require_role("admin")
def set_allow_writes():
    """Decide whether the model may ASK for a data-changing action.

    This is the only thing the checkbox controls. With it on, the write
    declarations are sent to the API and the model can propose one; with it
    off they are not sent at all, so there is nothing for it to propose.

    Neither setting lets anything run. A proposal becomes a change when a
    person clicks Approve, and nowhere else.
    """
    if not may_use_write_actions():
        return jsonify({"ok": False,
                        "error": "Your role cannot arm write actions."}), 403

    session.allow_writes = bool((request.json or {}).get("allow", False))
    return jsonify({"ok": True, "allow_writes": session.allow_writes,
                    "offered": sorted(coach_core.WRITE_TOOLS)
                    if session.allow_writes else []})


@app.post("/api/actions/<int:action_id>/decide")
@require_role("admin")
def decide_action(action_id):
    """Approve or reject one proposed write. THE approval point.

    This is the only place in the application that calls a write tool, and it
    is reachable only by an authenticated person POSTing a decision. The model
    cannot reach it: it has no HTTP client, and the function-calling loop
    never runs a tool marked `writes`.
    """
    if not may_use_write_actions():
        return jsonify({"ok": False,
                        "error": "Your role cannot approve actions."}), 403

    wanted = (request.json or {}).get("decision", "")
    if wanted not in (actions.APPROVED, actions.REJECTED):
        return jsonify({"ok": False,
                        "error": "Decision must be approved or rejected."}), 400

    proposal = actions.get(action_id)
    if proposal is None:
        return jsonify({"ok": False, "error": f"No action {action_id}."}), 404

    # The proposal belongs to a case, and the case has its own visibility
    # rules. Approving a refund on somebody else's case is not a thing.
    if proposal["case_id"]:
        case = next((c for c in load_cases()
                     if c.get("id") == proposal["case_id"]), None)
        if case is not None and not may_see_case(case):
            return jsonify({"ok": False,
                            "error": "That action is on another agent's case."}), 403

    # Recorded BEFORE the tool runs. If the write then fails, the trail still
    # shows that a person approved it, which is the fact being audited.
    decided, why = actions.decide(action_id, wanted, signed_in_username())
    if decided is None:
        return jsonify({"ok": False, "error": why}), 409

    if wanted == actions.REJECTED:
        return jsonify({"ok": True, "action": decided,
                        "state": session.as_dict()})

    tool = coach_core.BACK_OFFICE.get(decided["action"])
    if tool is None or not tool["writes"]:
        return jsonify({"ok": False,
                        "error": f"{decided['action']} is not a write tool."}), 400

    try:
        result = tool["run"](**(decided["args"] or {}))
    except Exception as error:
        result = {"error": f"{type(error).__name__}: {error}"}

    decided = actions.record_result(action_id, result)

    # The run is part of the case, so it survives a reload and a reopen.
    session.persist()
    return jsonify({"ok": True, "action": decided,
                    "state": session.as_dict()})


@app.get("/api/actions")
@require_role("admin")
def list_actions():
    """The audit trail, filtered the way the Cases table filters."""
    rows, matched = actions.listing(
        query=(request.args.get("q") or "").strip(),
        action=request.args.get("action", "all"),
        decision=request.args.get("decision", "all"),
        limit=int(request.args.get("limit", 200)),
    )
    return jsonify({"ok": True, "actions": rows, "shown": len(rows),
                    "matched": matched, "totals": actions.totals(),
                    "known": sorted(coach_core.WRITE_TOOLS)})


@app.get("/api/actions.csv")
@require_role("admin")
def export_actions_csv():
    """Download the audit trail, honouring whatever filters are active."""
    rows, _ = actions.listing(
        query=(request.args.get("q") or "").strip(),
        action=request.args.get("action", "all"),
        decision=request.args.get("decision", "all"),
        limit=100000,
    )

    columns = ["id", "at", "case_id", "username", "action", "description",
               "arguments", "reason", "decision", "decided_at", "decided_by",
               "reference", "outcome"]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        result = row.get("result") or {}
        writer.writerow({
            **{k: row.get(k, "") or "" for k in columns},
            "arguments": json.dumps(row.get("args") or {}),
            "outcome": result.get("error") or result.get("summary", ""),
        })

    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="support-coach-actions-{stamp}.csv"'
        },
    )


@app.post("/api/rate")
@require_role("agent")
def rate_suggestion():
    """Thumbs up or down on the suggestion currently on screen."""
    rating = (request.json or {}).get("rating")
    if rating not in ("up", "down", None):
        return jsonify({"ok": False, "error": "rating must be up or down."}), 400

    if not session.ratings:
        return jsonify({"ok": False, "error": "No suggestion to rate yet."}), 400

    # Rating the same suggestion again replaces the earlier verdict rather
    # than counting twice.
    session.ratings[-1]["rating"] = rating
    session.ratings[-1]["rated_at"] = now_iso()
    session.persist()

    return jsonify({"ok": True, "ratings": session.ratings,
                    "state": session.as_dict()})


@app.get("/api/state")
@require_role("agent")
def get_state():
    return jsonify({"ok": True, "state": session.as_dict()})


@app.post("/api/reset")
@require_role("agent")
def reset():
    """Start a new conversation. Anything already said stays as a pending case."""
    if session.case_id:
        session.persist()
    session.reset()
    return jsonify({"ok": True, "state": session.as_dict()})


def prepare_reopened_case(session):
    """Draft a reply, and score it, for a case that was saved without one.

    Cases whose draft failed at the time were saved with suggestion = "" --
    the empty string at the end of the draft's except branch -- and opening a
    case only ever replayed stored state. So those cases showed "No
    suggestion yet" for ever, however many times you opened them.

    This fills the gap once, on open, and persists the result, so opening the
    same case again costs nothing.

    Returns a dict describing what it produced, or None when there was
    nothing to do.
    """
    if session.last_suggestion or not session.last_customer_message:
        return None

    allowed, _why = metering.check_limits(signed_in_username())
    if not allowed:
        return None          # over budget: leave the panel honestly empty

    try:
        session.last_suggestion = session.coach.suggest_reply(
            session.last_customer_message,
            session.state.history,
            analysis={
                "sentiment": session.state.sentiment,
                "urgency": session.state.urgency,
                "key_issue": session.state.key_issue,
            },
            facts=session.facts,
        )
        note_redactions(session)
        article = getattr(session.coach, "last_article", None)
        open_rating_slot(session, article is not None,
                         article.get("topic") if article else None)
    except Exception as error:
        # Loud this time. A silently swallowed draft failure is exactly what
        # left these cases blank in the first place.
        print(f"  DRAFT on open failed for {session.case_id}: {error}")
        session.last_suggestion = ""
        return None

    # Score the draft so the scorecard has something to show before the agent
    # has written anything. Marked scored="draft" so nothing downstream
    # mistakes it for a human's reply -- see performance().
    scored_draft = False
    if not session.last_feedback:
        try:
            verdict = session.coach.evaluate_agent_response(
                session.last_customer_message, session.last_suggestion)
            session.last_feedback = {
                "tone_score": verdict.tone_score,
                "empathy_score": verdict.empathy_score,
                "clarity_score": verdict.clarity_score,
                "coaching_tip": verdict.coaching_tip,
                "scored": "draft",
            }
            scored_draft = True
        except Exception as error:
            print(f"  SCORING the draft failed for {session.case_id}: {error}")

    return {"drafted": True, "scored_draft": scored_draft}


@app.post("/api/open-case")
@require_role("agent")
def open_case():
    """Load a saved case into the console so the agent can carry on with it."""
    case_id = (request.json or {}).get("id", "")

    # Whatever is open now must be saved before we swap it out.
    if session.case_id and session.case_id != case_id:
        session.persist()

    for case in load_cases():
        if case.get("id") == case_id:
            if not may_see_case(case):
                return jsonify({"ok": False,
                                "error": "That case belongs to another agent."}), 403
            session.load(case)
            prepared = prepare_reopened_case(session)
            session.persist()      # record the claim, and keep the new draft
            return jsonify({"ok": True, "state": session.as_dict(),
                            "prepared": prepared})

    return jsonify({"ok": False, "error": f"No case {case_id}."}), 404


@app.post("/api/resolve")
@require_role("agent")
def resolve():
    """Mark the open case resolved, then start a fresh one."""
    if not session.case_id:
        return jsonify({"ok": False, "error": "Nothing to resolve yet."}), 400

    record = session.persist(status="resolved", closed_at=now_iso())
    session.reset()
    return jsonify({"ok": True, "resolved": record["id"], "state": session.as_dict()})


# ==========================================================================
# API -- the dashboard
# ==========================================================================
# The windows the Trends toggle offers. 24 hours buckets by HOUR: a day
# split into days is one or two bars, which is not a chart.
RANGES = {
    "24h": {"bucket": "hour", "size": 24},
    "7d":  {"bucket": "day",  "size": 7},
    "14d": {"bucket": "day",  "size": 14},
    "30d": {"bucket": "day",  "size": 30},
}
DEFAULT_RANGE = "7d"


def read_range(default=DEFAULT_RANGE):
    wanted = request.args.get("range", default)
    return wanted if wanted in RANGES else default


def window_starts(range_key):
    """Every bucket start in the window, oldest first, plus the step size.

    Both charts bucket through here. Two implementations of "the last 7 days"
    drift the moment one of them is edited, and the two charts sit one above
    the other where a one-bucket disagreement is plainly visible.
    """
    spec = RANGES.get(range_key, RANGES[DEFAULT_RANGE])
    hourly = spec["bucket"] == "hour"
    now = datetime.now(UTC)

    step = timedelta(hours=1) if hourly else timedelta(days=1)
    latest = (now.replace(minute=0, second=0, microsecond=0) if hourly
              else datetime.combine(now.date(), datetime.min.time())
                           .replace(tzinfo=UTC))
    return [latest - step * i for i in range(spec["size"] - 1, -1, -1)], step, hourly


def bucket_start(when, hourly):
    """The bucket a timestamp belongs in."""
    return (when.replace(minute=0, second=0, microsecond=0) if hourly
            else datetime.combine(when.date(), datetime.min.time())
                         .replace(tzinfo=UTC))


def read_when(case):
    """A case's opened_at as an aware datetime, or None if unreadable."""
    opened = case.get("opened_at")
    if not opened:
        return None
    try:
        return datetime.fromisoformat(opened).astimezone(UTC)
    except ValueError:
        return None       # a timestamp we cannot read is not worth a crash


def timeline(cases, range_key=DEFAULT_RANGE):
    """Cases per bucket across a fixed window, ending now.

    Quiet buckets are filled with zeros rather than dropped -- a gap in the
    middle of a chart is information, and leaving it out would quietly
    compress the time axis and make volume look steadier than it was.

    Returns [] only when NOTHING in the store has a readable date. A window
    with no cases in it still returns its empty buckets, so a quiet 24 hours
    draws an empty axis rather than claiming there are no cases at all.
    """
    starts, step, hourly = window_starts(range_key)
    window_start, latest = starts[0], starts[-1]

    blank = {"resolved": 0, "pending": 0,
             "ai_autonomous": 0, "hybrid": 0, "human": 0}
    buckets = {s: dict(blank) for s in starts}

    usable = 0
    for case in cases:
        when = read_when(case)
        if when is None:
            continue

        usable += 1
        if when < window_start or when > latest + step:
            continue          # outside the window the viewer asked for

        counts = buckets.get(bucket_start(when, hourly))
        if counts is None:
            continue

        counts["resolved" if case.get("status") == "resolved"
               else "pending"] += 1

        mode = case.get("resolution_mode") or resolution_mode_for(case)
        if mode in counts:
            counts[mode] += 1

    if not usable:
        return []

    out = []
    for start in starts:
        counts = buckets[start]
        out.append({
            "at": start.isoformat(),
            "day": start.date().isoformat(),
            "resolved": counts["resolved"],
            "pending": counts["pending"],
            "total": counts["resolved"] + counts["pending"],
            "ai_autonomous": counts["ai_autonomous"],
            "hybrid": counts["hybrid"],
            "human": counts["human"],
        })
    return out


# Worst first: an angry customer waiting is more urgent than a calm one
# waiting the same length of time.
RISK_ORDER = {"high": 3, "medium": 2, "low": 1}


def work_queue(cases, limit=8):
    """Open cases, ranked by what an agent should pick up next.

    Sorted by escalation risk first, then by age -- so the oldest of the
    riskiest conversations sits at the top.
    """
    now = datetime.now(UTC)
    waiting = []

    for case in cases:
        if case.get("status") in ("resolved", "auto_resolved"):
            continue

        opened = parse_time(case.get("opened_at"))
        waiting.append({
            "id": case.get("id"),
            "escalation_risk": case.get("escalation_risk"),
            "frustration": case.get("frustration", 0),
            "turns": case.get("turns", 0),
            "opened_at": case.get("opened_at"),
            "age_seconds": int((now - opened).total_seconds()) if opened else 0,
            "key_issue": case.get("key_issue", ""),
            "first_message": next(
                (m["text"] for m in case.get("messages", [])
                 if m.get("speaker") == "customer"), ""),
            "sla": sla_for(case, now),
            "awaiting_reply": case.get("first_response_at") is None,
        })

    waiting.sort(key=lambda row: (
        -RISK_ORDER.get(row["escalation_risk"], 0),   # riskiest first
        -row["age_seconds"],                          # then oldest first
    ))

    return {"queue": waiting[:limit], "total": len(waiting)}


def sla_summary(cases, limit=6):
    """Counts by SLA status, plus the cases that need attention first."""
    now = datetime.now(UTC)
    counts = {"met": 0, "breached": 0, "at_risk": 0,
              "on_track": 0, "unknown": 0}
    waiting = []

    for case in cases:
        sla = sla_for(case, now)
        counts[sla["status"]] = counts.get(sla["status"], 0) + 1

        # Only OPEN cases can still be saved, so they are the ones worth
        # putting in front of somebody.
        if (case.get("status") not in ("resolved", "auto_resolved")
                and sla["status"] in ("breached", "at_risk")):
            waiting.append({
                "id": case.get("id"),
                "escalation_risk": case.get("escalation_risk"),
                "key_issue": case.get("key_issue", ""),
                "first_message": next(
                    (m["text"] for m in case.get("messages", [])
                     if m.get("speaker") == "customer"), ""),
                "sla": sla,
            })

    # Worst first: most overdue at the top.
    waiting.sort(key=lambda row: row["sla"]["remaining_seconds"] or 0)

    answered = counts["met"] + counts["breached"]
    return {
        "counts": counts,
        "on_time_rate": round(100 * counts["met"] / answered) if answered else 0,
        "breaching": waiting[:limit],
        "breaching_total": len(waiting),
    }


# Coaching tips are free text, so we group them the same honest way the
# knowledge base does: keyword overlap. A tip can land in more than one theme,
# which is correct -- "add a greeting and give a timeline" really is both.
TIP_THEMES = [
    ("Open with a greeting",
     ["greeting", "greet", "hello", "opening line", "open with"]),
    ("Acknowledge the problem first",
     ["acknowledge", "empathy", "empathetic", "understand", "frustration",
      "stress", "feelings", "validate", "reassure"]),
    ("Give a specific timeline",
     ["timeline", "timeframe", "time frame", "how long", "specific date",
      "deadline", "when the", "within"]),
    ("Apologise for the delay",
     ["apolog", "sorry", "delay", "wait time", "kept waiting"]),
    ("Be specific, not generic",
     ["specific", "generic", "actual issue", "personalise", "personalize",
      "vague", "tailor", "detail"]),
    ("Do not ask for details too soon",
     ["before asking", "straight to", "jumping", "instead of asking",
      "rather than asking", "immediately asking"]),
]


def suggestion_quality(cases):
    """How often the agent actually accepted what we suggested.

    Split by whether a help article was found, because that is the thing we
    most want to know: does grounding a suggestion in real documentation make
    an agent more likely to use it?
    """
    groups = {"grounded": {"up": 0, "down": 0},
              "ungrounded": {"up": 0, "down": 0}}
    produced = rated = 0

    for case in cases:
        for entry in case.get("ratings") or []:
            produced += 1
            verdict = entry.get("rating")
            if verdict not in ("up", "down"):
                continue
            rated += 1
            bucket = "grounded" if entry.get("grounded") else "ungrounded"
            groups[bucket][verdict] += 1

    def rate(counts):
        total = counts["up"] + counts["down"]
        return {"up": counts["up"], "down": counts["down"], "total": total,
                "rate": round(100 * counts["up"] / total) if total else None}

    overall = {"up": sum(g["up"] for g in groups.values()),
               "down": sum(g["down"] for g in groups.values())}

    return {
        "produced": produced,
        "rated": rated,
        "overall": rate(overall),
        "grounded": rate(groups["grounded"]),
        "ungrounded": rate(groups["ungrounded"]),
    }


def performance(cases, range_key=None):
    """How the agent is doing, and which way it is going.

    Every case carries at most one scorecard, so a "point" here is one scored
    conversation rather than one reply. With a handful of cases that is the
    honest unit -- pretending to per-reply resolution would be inventing
    detail we do not have.
    """
    # A scorecard marked scored="draft" rates a reply the coach wrote and
    # nobody sent. Counting it here would mix the model's writing into "how
    # the agent is doing", which is the one thing this panel must not do.
    scored = [c for c in cases
              if c.get("feedback")
              and (c["feedback"] or {}).get("scored") != "draft"]
    scored.sort(key=lambda c: c.get("opened_at") or "")

    # The chart follows the range toggle; the trend and the themes below do
    # not. "Improving" judged over whatever window happens to be selected
    # would flip direction as you click between them, which is a worse
    # number than no number.
    def in_window(rows):
        if not range_key:
            return rows
        starts, step, _ = window_starts(range_key)
        first, last = starts[0], starts[-1] + step
        return [c for c in rows
                if (w := read_when(c)) is not None and first <= w <= last]

    charted = in_window(scored)
    # CSAT rides the same axis, but a rated case is not necessarily a scored
    # one -- an auto-resolved case has a rating and no scorecard.
    charted_csat = in_window(rated(cases))

    fields = ("tone_score", "empathy_score", "clarity_score")
    short = {"tone_score": "tone", "empathy_score": "empathy",
             "clarity_score": "clarity"}

    if not scored and not charted_csat:
        return {"scored": 0, "averages": {}, "by_day": [],
                "trend": {}, "themes": [], "enough_for_trend": False}

    def mean(rows, field):
        values = [r["feedback"].get(field) for r in rows
                  if isinstance(r["feedback"].get(field), (int, float))]
        return round(sum(values) / len(values), 2) if values else None

    # ---- averaged per bucket, skipping buckets with no scores ----
    # An empty bucket is left out rather than plotted as a gap at zero: no
    # conversation was scored, which is not the same as one scoring nothing.
    hourly = bool(range_key) and RANGES.get(
        range_key, RANGES[DEFAULT_RANGE])["bucket"] == "hour"

    def bucket_key(case):
        when = read_when(case)
        if when is None:
            return None
        return (bucket_start(when, hourly).isoformat() if range_key
                else (case.get("opened_at") or "")[:10])

    buckets, csat_buckets = {}, {}
    for case in charted:
        key = bucket_key(case)
        if key:
            buckets.setdefault(key, []).append(case)
    for case in charted_csat:
        key = bucket_key(case)
        if key:
            csat_buckets.setdefault(key, []).append(case)

    by_day = []
    for key in sorted(set(buckets) | set(csat_buckets)):
        rows = buckets.get(key, [])
        rated_rows = csat_buckets.get(key, [])
        stars = [r["csat_score"] for r in rated_rows]
        by_day.append({
            "at": key,
            "day": key[:10],
            "n": len(rows),
            **{short[f]: mean(rows, f) for f in fields},
            # None rather than 0 for a bucket nobody rated -- no rating is
            # not a rating of nothing, and the line should break, not dive.
            "csat": round(sum(stars) / len(stars), 2) if stars else None,
            "csat_n": len(stars),
        })

    # ---- which way is it going? ----
    # Compare the older half against the newer half. With an odd count the
    # middle case is left out of both, so it cannot skew either side.
    trend = {}
    enough = len(scored) >= 4
    if enough:
        half = len(scored) // 2
        older, newer = scored[:half], scored[-half:]
        for field in fields:
            before, after = mean(older, field), mean(newer, field)
            if before is None or after is None:
                continue
            delta = round(after - before, 2)
            trend[short[field]] = {
                "before": before, "after": after, "delta": delta,
                "direction": "improving" if delta >= 0.5
                             else "declining" if delta <= -0.5
                             else "steady",
            }

    # ---- what the coach keeps saying ----
    themes = []
    for label, keywords in TIP_THEMES:
        hits = [c for c in scored
                if any(word in (c["feedback"].get("coaching_tip") or "").lower()
                       for word in keywords)]
        if hits:
            themes.append({
                "label": label, "count": len(hits),
                "example": hits[-1]["feedback"].get("coaching_tip", ""),
                "cases": [h["id"] for h in hits][:4],
            })
    themes.sort(key=lambda t: -t["count"])

    return {
        "scored": len(scored),
        "averages": {short[f]: mean(scored, f) for f in fields},
        "by_day": by_day,
        "trend": trend,
        "enough_for_trend": enough,
        "themes": themes,
        "tips_total": len(scored),
    }


@app.post("/api/cases/<case_id>/csat")
@require_role("agent")
def rate_case(case_id):
    """Record the customer's rating of one resolved case.

    Guarded by case visibility rather than by a role of its own, so it starts
    working for a customer the moment the customer role and the portal exist
    -- may_see_case() is where "their own ticket" will be answered.
    """
    body = request.json or {}

    for case in load_cases():
        if case.get("id") != case_id:
            continue
        if not may_see_case(case):
            return jsonify({"ok": False,
                            "error": "That case belongs to someone else."}), 403

        updated, why = record_csat(case, body.get("score"),
                                   body.get("comment", ""))
        if updated is None:
            return jsonify({"ok": False, "error": why}), 400

        save_case(updated)
        return jsonify({"ok": True, "case_id": case_id,
                        "csat_score": updated["csat_score"],
                        "csat_comment": updated["csat_comment"],
                        "csat_at": updated["csat_at"],
                        "editable_for_hours": CSAT_EDIT_HOURS})

    return jsonify({"ok": False, "error": f"No case {case_id}."}), 404


@app.get("/api/queue")
@require_role("agent")
def queue_view():
    """The work queue, for the console's third column.

    /api/stats already carries a queue, but it is lead-only and the console
    is an agent's page -- so this exists to widen the audience, NOT to rank
    anything. The ordering is work_queue()'s, unchanged: one ranking, two
    callers. A second sort written here would drift from the dashboard's and
    the two views would disagree about what to pick up next.

    Cases are filtered BEFORE ranking, so an agent's queue holds only what
    they can actually open. Ranking first and hiding afterwards would leave
    gaps in the list and a count that disagreed with it.
    """
    limit = max(1, min(int(request.args.get("limit", 50)), 200))
    visible = [case for case in load_cases() if may_see_case(case)]

    result = work_queue(visible, limit=limit)
    result["ok"] = True
    result["open_case"] = session.case_id
    return jsonify(result)


@app.get("/api/performance")
@require_role("admin")
def performance_view():
    cases = load_cases()
    chosen_range = read_range()
    return jsonify({"ok": True,
                    "range": chosen_range,
                    "bucket": RANGES[chosen_range]["bucket"],
                    **performance(cases, chosen_range),
                    "suggestions": suggestion_quality(cases)})


@app.get("/api/stats")
@require_role("admin")
def stats():
    """Everything the dashboard needs, counted server-side."""
    cases = load_cases()
    # Only the timeline honours the range. The headline counts stay all-time:
    # "43 resolved" quietly meaning "43 this week" would be a different number
    # wearing the same label.
    chosen_range = read_range()

    resolved = [c for c in cases if c.get("status") == "resolved"]
    deflected = [c for c in cases if c.get("status") == "auto_resolved"]
    pending = [c for c in cases
               if c.get("status") not in ("resolved", "auto_resolved")]

    def count_by(field, values, source):
        return {v: sum(1 for c in source if c.get(field) == v) for v in values}

    # Who closed what. Only cases that are actually closed have a resolver,
    # so an open case waiting on a reply cannot quietly count as "AI handled".
    closed = resolved + deflected
    by_mode = {m: 0 for m in RESOLUTION_MODES}
    for case in closed:
        mode = case.get("resolution_mode") or resolution_mode_for(case)
        if mode in by_mode:
            by_mode[mode] += 1

    decided = sum(by_mode.values())
    # How much of the closed work the split actually describes. A case closed
    # without an outgoing reply has no resolver, so the three modes cover
    # fewer cases than "resolved + deflected" -- and presenting the split
    # without saying so reads as though it covered all of them.
    eligible_for_mode = len(closed)
    ai_end_to_end = round(100 * by_mode["ai_autonomous"] / decided) if decided else 0

    total_turns = sum(c.get("turns", 0) for c in cases)
    total_calls = sum(c.get("calls", 0) for c in cases)

    return jsonify({
        "ok": True,
        "total": len(cases),
        "resolved": len(resolved),
        "pending": len(pending),
        "deflected": len(deflected),
        "deflection_rate": round(100 * len(deflected) / len(cases)) if cases else 0,
        "resolution_rate": round(
            100 * (len(resolved) + len(deflected)) / len(cases)) if cases else 0,
        "high_risk_pending": sum(
            1 for c in pending if c.get("escalation_risk") == "high"
        ),
        "by_risk": count_by("escalation_risk", ["low", "medium", "high"], cases),
        "by_sentiment": count_by(
            "sentiment", ["positive", "neutral", "negative"], cases
        ),
        "pending_by_risk": count_by(
            "escalation_risk", ["low", "medium", "high"], pending
        ),
        "csat": csat_summary(cases),
        "csat_by_mode": csat_by_mode(cases),
        "first_reply": median_first_reply(cases),
        "categories": category_volume(cases),
        "by_mode": by_mode,
        "modes_counted": decided,
        "modes_eligible": eligible_for_mode,
        # The headline pair. Human touch rate is the exact inverse, computed
        # from the same denominator so the two always sum to 100 -- two
        # independent roundings would sometimes show 61% and 40%.
        "ai_end_to_end": ai_end_to_end,
        "human_touch_rate": 100 - ai_end_to_end if decided else 0,
        "sla": sla_summary(cases),
        "work_queue": work_queue(cases),
        "range": chosen_range,
        "bucket": RANGES[chosen_range]["bucket"],
        "by_day": timeline(cases, chosen_range),
        "avg_turns": round(total_turns / len(cases), 1) if cases else 0,
        "total_calls": total_calls,
        "open_case": session.case_id,
    })


def filter_cases(cases, query="", status="all", risk="all", sentiment="all",
                 mode="all"):
    """Narrow the case list. Every filter defaults to "all" = no filtering.

    The search runs over the case id, the key issue AND every message, which
    is why it lives here rather than in the browser: /api/cases deliberately
    strips the transcript out of its response, so the front end never has the
    full text to search.
    """
    query = (query or "").strip().lower()
    kept = []

    for case in cases:
        if status != "all" and case.get("status") != status:
            continue
        if risk != "all" and case.get("escalation_risk") != risk:
            continue
        if sentiment != "all" and case.get("sentiment") != sentiment:
            continue
        if mode != "all" and (case.get("resolution_mode")
                              or resolution_mode_for(case)) != mode:
            continue

        if query:
            haystack = " ".join([
                str(case.get("id", "")),
                str(case.get("key_issue", "")),
                *(m.get("text", "") for m in case.get("messages", [])),
            ]).lower()
            if query not in haystack:
                continue

        kept.append(case)

    return kept


def read_filters():
    """Pull the four filter values off the query string."""
    return {
        "query": request.args.get("q", ""),
        "status": request.args.get("status", "all"),
        "risk": request.args.get("risk", "all"),
        "sentiment": request.args.get("sentiment", "all"),
        "mode": request.args.get("mode", "all"),
    }


@app.get("/api/faqs")
@require_role("agent")
def faqs():
    """The canned questions, each with how many saved cases look like it.

    Matching is deliberately simple keyword overlap -- the same honest
    approach the notebook's knowledge base uses.
    """
    cases = load_cases()

    def haystack(case):
        parts = [case.get("key_issue", "")]
        parts += [m.get("text", "") for m in case.get("messages", [])]
        return " ".join(parts).lower()

    blobs = [haystack(c) for c in cases]

    rows = []
    for faq in FAQS:
        count = sum(
            1 for blob in blobs
            if any(word in blob for word in faq["keywords"])
        )
        rows.append({
            "id": faq["id"], "icon": faq["icon"], "label": faq["label"],
            "text": faq["text"], "count": count,
        })

    # Busiest topic first on the dashboard; the console keeps its own order.
    return jsonify({
        "ok": True,
        "faqs": rows,
        "ranked": sorted(rows, key=lambda r: -r["count"]),
        "matched": sum(1 for b in blobs if any(
            w in b for f in FAQS for w in f["keywords"])),
        "total_cases": len(cases),
    })


@app.get("/api/cases")
@require_role("admin")
def list_cases():
    """Recent cases, newest first, without the full message transcript."""
    limit = int(request.args.get("limit", 25))
    all_cases = load_cases()
    cases = filter_cases(all_cases, **read_filters())

    slim = []
    for case in reversed(cases):
        row = {k: v for k, v in case.items() if k != "messages"}
        first = next(
            (m["text"] for m in case.get("messages", [])
             if m["speaker"] == "customer"),
            "",
        )
        row["first_message"] = first
        row["is_open"] = case.get("id") == session.case_id
        row["sla"] = sla_for(case)
        slim.append(row)

    # One query for the whole page rather than one per row.
    shown = slim[:limit]
    costs = metering.costs_for_cases([r["id"] for r in shown if r.get("id")])
    for row in shown:
        row["usage"] = costs.get(row.get("id"),
                                 {"tokens": 0, "cost_inr": 0})

    return jsonify({
        "ok": True,
        "cases": shown,
        "shown": min(limit, len(slim)),
        "matched": len(slim),        # after filtering
        "total": len(all_cases),     # before filtering
    })


@app.get("/api/usage")
@require_role("admin")
def usage():
    """What the model is costing: today against the cap, by day, by step,
    and the conversations that spent the most."""
    return jsonify({"ok": True, **metering.summary()})


@app.get("/api/gaps")
@require_role("admin")
def knowledge_gaps():
    """Every question our documentation could not answer, most asked first."""
    grouped = {}

    for case in load_cases():
        for entry in case.get("unanswered", []):
            text = (entry.get("text") or "").strip()
            if not text:
                continue

            # Group on a squashed version so the same question asked twice
            # counts twice, but we still display it as it was actually typed.
            key = " ".join(text.lower().split())
            row = grouped.setdefault(key, {
                "text": text, "count": 0, "cases": [], "last_seen": None,
            })
            row["count"] += 1
            if case.get("id") not in row["cases"]:
                row["cases"].append(case.get("id"))
            seen = entry.get("at") or case.get("opened_at")
            if seen and (row["last_seen"] is None or seen > row["last_seen"]):
                row["last_seen"] = seen

    rows = sorted(
        grouped.values(),
        key=lambda r: (-r["count"], r["last_seen"] or ""),
    )

    total_questions = sum(
        1 for c in load_cases()
        for m in c.get("messages", []) if m.get("speaker") == "customer"
    )

    return jsonify({
        "ok": True,
        "gaps": rows,
        "distinct": len(rows),
        "logged": sum(r["count"] for r in rows),
        "customer_messages": total_questions,
    })


@app.get("/api/export.json")
@require_role("admin")
def export_json():
    """Everything, in the exact shape cases.json always had.

    The store is SQLite now, but this format is still what the migration
    reads and what a backup looks like, so it stays supported.
    """
    cases = filter_cases(load_cases(), **read_filters())
    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(
        json.dumps({"cases": cases}, indent=1),
        mimetype="application/json",
        headers={"Content-Disposition":
                 f'attachment; filename="support-coach-cases-{stamp}.json"'},
    )


@app.get("/api/cases.csv")
@require_role("admin")
def export_csv():
    """Download the cases as a spreadsheet.

    With no filters this is every case, which is the normal use. If filters
    are active it exports exactly what you are looking at, so what you see is
    what you get.
    """
    cases = filter_cases(load_cases(), **read_filters())

    columns = [
        "id", "status", "opened_at", "closed_at", "escalation_risk",
        "frustration", "trend", "sentiment", "urgency", "turns", "calls",
        "key_issue", "first_customer_message",
        "tone_score", "empathy_score", "clarity_score", "coaching_tip",
        "suggested_reply",
    ]

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()

    for case in cases:
        feedback = case.get("feedback") or {}
        first = next(
            (m["text"] for m in case.get("messages", [])
             if m.get("speaker") == "customer"),
            "",
        )
        writer.writerow({
            **{k: case.get(k, "") for k in columns},
            "first_customer_message": first,
            "tone_score": feedback.get("tone_score", ""),
            "empathy_score": feedback.get("empathy_score", ""),
            "clarity_score": feedback.get("clarity_score", ""),
            "coaching_tip": feedback.get("coaching_tip", ""),
            "suggested_reply": case.get("suggestion", "") or "",
        })

    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(
        buffer.getvalue(),
        mimetype="text/csv",
        headers={
            "Content-Disposition":
                f'attachment; filename="support-coach-cases-{stamp}.csv"'
        },
    )


@app.get("/api/cases/<case_id>")
@require_role("agent")
def one_case(case_id):
    for case in load_cases():
        if case.get("id") == case_id:
            if not may_see_case(case):
                # 404, not 403. Telling an agent a case exists but is not
                # theirs still tells them it exists.
                return jsonify({"ok": False, "error": "No such case."}), 404
            case = dict(case, usage=metering.for_case(case_id))
            return jsonify({"ok": True, "case": case})
    return jsonify({"ok": False, "error": "No such case."}), 404


# ==========================================================================
# Start-up
# ==========================================================================
def find_free_port(preferred, attempts=12):
    """Return the first free port at or after `preferred`, or None."""
    for offset in range(attempts):
        port = preferred + offset
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            probe.close()
    return None


def who_has_the_port(port):
    """Best-effort: name the process holding a port, for the error message."""
    try:
        output = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True, text=True, timeout=4,
            check=False,          # lsof exits non-zero when nothing matches

        ).stdout.strip().splitlines()
        if len(output) > 1:
            columns = output[1].split()
            return f"{columns[0]} (pid {columns[1]})"
    except Exception:
        pass
    return "another program"


# ==========================================================================
# Account management from the command line
# ==========================================================================
def run_cli(argv):
    """`python3 app/server.py <command>` for the account commands.

    Accounts are deliberately not creatable through the web app: an app that
    can mint its own admin is one request away from not having roles at all.
    """
    import getpass

    command = argv[0]
    init_db()

    if command == "--list-users":
        users = auth.list_users()
        if not users:
            print("\n  No accounts yet. The first one is created on startup.\n")
            return 0
        print()
        print(f"  {'USERNAME':<16}{'ROLE':<8}{'ACTIVE':<9}{'LAST SIGNED IN'}")
        print("  " + "-" * 58)
        for u in users:
            state = "locked" if u["locked"] else ("yes" if u["active"] else "no")
            print(f"  {u['username']:<16}{u['role']:<8}{state:<9}"
                  f"{u['last_login_at'] or 'never'}")
        print()
        return 0

    if command in ("--add-user", "--passwd", "--disable-user", "--enable-user"):
        username = argv[1] if len(argv) > 1 else input("  username: ").strip()

        try:
            if command == "--disable-user":
                auth.set_active(username, False)
                print(f"\n  {username} can no longer sign in.\n")
                return 0

            if command == "--enable-user":
                auth.set_active(username, True)
                print(f"\n  {username} can sign in again.\n")
                return 0

            if command == "--add-user":
                role = argv[2] if len(argv) > 2 else ""
                while role not in auth.ROLES:
                    print("\n  Roles: " + ", ".join(
                        f"{r} ({auth.ROLE_SUMMARY[r]})" for r in auth.ROLES))
                    role = input("  role: ").strip().lower()

            password = getpass.getpass("  password (min 8 chars): ")
            if password != getpass.getpass("  again: "):
                print("\n  Those did not match.\n")
                return 1

            if command == "--add-user":
                auth.create_user(username, password, role)
                print(f"\n  Created {username} as {role}.\n")
            else:
                auth.set_password(username, password)
                print(f"\n  Password changed for {username}.\n")
            return 0

        except ValueError as problem:
            print(f"\n  {problem}\n")
            return 1

    print(f"\n  Unknown command: {command}")
    print("""
  Account commands:
      --list-users
      --add-user [username] [customer|agent|admin]
      --passwd [username]
      --disable-user [username]
      --enable-user [username]
""")
    return 1


if __name__ == "__main__":

    if len(sys.argv) > 1 and sys.argv[1].startswith("--"):
        raise SystemExit(run_cli(sys.argv[1:]))


    # Port 5000 is taken by AirPlay Receiver on macOS, so we start at 5001.
    # Override with:  PORT=8080 python3 app/server.py
    preferred = int(os.getenv("PORT", "5001"))
    port = find_free_port(preferred)

    if port is None:
        print(f"\n  Could not find a free port between {preferred} and "
              f"{preferred + 11}.")
        print("  Free one up, or choose your own:  PORT=8080 python3 app/server.py\n")
        raise SystemExit(1)

    imported = migrate_from_json()
    backfilled = backfill_resolution_modes()

    # Cookie settings and the signing key, then the first admin if the user
    # table is empty. Both need the database, so they come after the migrate.
    auth.harden(app, local_only=True)
    seeded = auth.ensure_seed_admin()
    moved = migrate_retired_roles()
    # One documented password for the demo accounts, so the login page's
    # one-click buttons can fill it. A convenience for presenting, not a
    # security design -- change it with --passwd, or set DEMO_PASSWORD.
    demo_password = os.getenv("DEMO_PASSWORD", "support-coach-demo")
    demo_made = auth.ensure_demo_users(demo_password)

    key_file = coach_core.find_key_file()
    saved = load_cases()

    print()
    print("  AI Support Coach")
    print("  " + "-" * 46)
    print(f"  key file : {key_file or 'NOT FOUND - see gemini_api_key.txt'}")
    print(f"  model    : {os.getenv('GEMINI_MODEL', 'gemini-3.5-flash-lite')}")
    print(f"  store    : sqlite  {os.path.basename(CASES_DB)}")
    if imported:
        print(f"  migrated : {imported} case(s) imported from cases.json")
    if backfilled:
        print(f"  backfill : resolution mode set on {backfilled} older case(s)")
    for username, was, now in moved:
        print(f"  role     : {username} moved from {was} to {now}")
    if demo_made:
        print(f"  seeded   : {', '.join(demo_made)} "
              f"(password: {demo_password})")
    print(f"  cases    : {len(saved)} saved "
          f"({sum(1 for c in saved if c.get('status') == 'resolved')} resolved)")

    if port != preferred:
        print(f"  note     : port {preferred} is busy "
              f"({who_has_the_port(preferred)}), using {port} instead")
        print("             to reclaim it:  pkill -f app/server.py")

    print(f"  accounts : {auth.user_count()} "
          f"(python3 app/server.py --list-users)")
    print(f"  console  : http://127.0.0.1:{port}")
    print(f"  dashboard: http://127.0.0.1:{port}/dashboard")

    if seeded:
        username, generated = seeded
        print()
        print("  " + "=" * 58)
        print("  FIRST RUN -- an admin account has been created.")
        print(f"      username: {username}")
        if generated:
            print(f"      password: {generated}")
            print()
            print("  This password is shown once and is not recoverable -- only")
            print("  its hash is stored. Write it down now. To change it:")
            print("      python3 app/server.py --passwd " + username)
        else:
            print("      password: the one in ADMIN_PASSWORD")
        print()
        print("  Then add the people who will actually use this:")
        print("      python3 app/server.py --add-user priya agent")
        print("      python3 app/server.py --add-user ravi agent")
        print("  " + "=" * 58)

    print()

    app.run(host="127.0.0.1", port=port, debug=False)
