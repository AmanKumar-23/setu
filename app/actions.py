"""Proposed write actions, and the audit trail of what was decided.

The model can ask for a write. It can never perform one. Between the asking
and the doing sits a row in this table and a person clicking a button.

Every proposal is recorded the moment it is made -- including the ones nobody
ever approves. A trail that only listed the actions that happened would be
useless for the question anyone actually asks afterwards, which is "what did
it try to do, and who stopped it?"
"""

import json
import sqlite3
from datetime import UTC, datetime

# Decisions a row can be in. "pending" is the state a proposal is born in.
PENDING, APPROVED, REJECTED = "pending", "approved", "rejected"
DECISIONS = (PENDING, APPROVED, REJECTED)

# What each action is called in front of a person, and how its arguments read
# as a sentence. The UI shows "Refund INR 499 on order OD-4471", not
# initiate_refund({"order_id": "OD-4471", "amount": 499}) -- somebody
# approving an action has to understand it without reading JSON.
LABELS = {
    "initiate_refund":     "Initiate refund",
    "expedite_delivery":   "Expedite delivery",
    "reset_account_access": "Reset account access",
}


def describe(action, args):
    """The action and its arguments as one plain-English line."""
    args = args or {}
    order = args.get("order_id")

    if action == "initiate_refund":
        amount = args.get("amount")
        money = f"INR {amount}" if amount not in (None, "") else "the full amount"
        return f"Refund {money} on order {order}"

    if action == "expedite_delivery":
        return f"Expedite delivery on order {order}"

    if action == "reset_account_access":
        who = args.get("customer_id")
        return f"Send a fresh sign-in link to {who}" if who else \
               "Send the customer a fresh sign-in link"

    # An action added later still reads as something rather than crashing the
    # card it was going to be rendered in.
    detail = ", ".join(f"{k}={v}" for k, v in args.items())
    return f"{LABELS.get(action, action)}{' ' + detail if detail else ''}"


SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    day        TEXT NOT NULL,
    case_id    TEXT,
    username   TEXT,
    action     TEXT NOT NULL,
    args       TEXT NOT NULL DEFAULT '{}',
    reason     TEXT,
    decision   TEXT NOT NULL DEFAULT 'pending',
    decided_at TEXT,
    decided_by TEXT,
    reference  TEXT,
    result     TEXT
);
CREATE INDEX IF NOT EXISTS actions_case ON actions(case_id);
CREATE INDEX IF NOT EXISTS actions_day  ON actions(day);
CREATE INDEX IF NOT EXISTS actions_dec  ON actions(decision, at);
"""

_connect = None


def configure(connect):
    """Hand this module the case store's connection factory."""
    global _connect
    _connect = connect


def connect():
    if _connect is None:
        raise RuntimeError("actions.configure(connect) was never called")
    return _connect()


def now_iso():
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row(r):
    args = json.loads(r["args"] or "{}")
    return {
        "id": r["id"], "at": r["at"], "day": r["day"],
        "case_id": r["case_id"], "username": r["username"],
        "action": r["action"], "args": args,
        "label": LABELS.get(r["action"], r["action"]),
        "description": describe(r["action"], args),
        "reason": r["reason"], "decision": r["decision"],
        "decided_at": r["decided_at"], "decided_by": r["decided_by"],
        "reference": r["reference"],
        "result": json.loads(r["result"]) if r["result"] else None,
    }


# --------------------------------------------------------------------------
# Proposing
# --------------------------------------------------------------------------
def propose(case_id, username, action, args, reason=None):
    """Record that the model asked for a write. Returns the new row.

    Recorded whether or not anybody ever decides on it, and recorded BEFORE
    the agent is shown the card -- so a proposal cannot be acted on without
    first existing in the trail.
    """
    at = now_iso()
    with connect() as conn:
        cursor = conn.execute("""
            INSERT INTO actions (at, day, case_id, username, action, args,
                                 reason, decision)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (at, at[:10], case_id, username, action,
              json.dumps(args or {}), reason, PENDING))
        new_id = cursor.lastrowid
    return get(new_id)


def get(action_id):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM actions WHERE id = ?", (action_id,)).fetchone()
    return _row(row) if row else None


def pending_for_case(case_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE case_id = ? AND decision = ? "
            "ORDER BY id", (case_id, PENDING)).fetchall()
    return [_row(r) for r in rows]


def for_case(case_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM actions WHERE case_id = ? ORDER BY id",
            (case_id,)).fetchall()
    return [_row(r) for r in rows]


# --------------------------------------------------------------------------
# Deciding
# --------------------------------------------------------------------------
def decide(action_id, decision, username):
    """Move a pending proposal to approved or rejected.

    Returns (row, None) or (None, why). Refuses anything already decided --
    without that check, a double-clicked Approve would run the write twice,
    and a refund is not an operation you want to be casual about repeating.
    """
    if decision not in (APPROVED, REJECTED):
        return None, f"{decision!r} is not a decision."

    current = get(action_id)
    if current is None:
        return None, f"No action {action_id}."
    if current["decision"] != PENDING:
        return None, (f"That action was already {current['decision']}"
                      f"{' by ' + current['decided_by'] if current['decided_by'] else ''}.")

    with connect() as conn:
        conn.execute("""
            UPDATE actions SET decision = ?, decided_at = ?, decided_by = ?
            WHERE id = ? AND decision = ?
        """, (decision, now_iso(), username, action_id, PENDING))

    return get(action_id), None


def record_result(action_id, result):
    """Store what the approved run actually returned."""
    reference = (result or {}).get("reference")
    with connect() as conn:
        conn.execute(
            "UPDATE actions SET reference = ?, result = ? WHERE id = ?",
            (reference, json.dumps(result or {}), action_id))
    return get(action_id)


# --------------------------------------------------------------------------
# The audit trail, for the dashboard
# --------------------------------------------------------------------------
def listing(query="", action="all", decision="all", limit=200):
    """The trail, newest first, filtered the way the Cases table filters."""
    sql = "SELECT * FROM actions"
    where, params = [], []

    if action != "all":
        where.append("action = ?")
        params.append(action)
    if decision != "all":
        where.append("decision = ?")
        params.append(decision)
    if query:
        where.append("(case_id LIKE ? OR username LIKE ? OR action LIKE ? "
                     "OR args LIKE ? OR reference LIKE ? OR reason LIKE ?)")
        params.extend([f"%{query}%"] * 6)

    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    matched = len(rows)
    return [_row(r) for r in rows[:limit]], matched


def totals():
    """Counts per decision, for the section badge and the header line."""
    try:
        with connect() as conn:
            rows = conn.execute(
                "SELECT decision, COUNT(*) n FROM actions GROUP BY decision"
            ).fetchall()
    except sqlite3.Error:
        return {d: 0 for d in DECISIONS}
    counts = {d: 0 for d in DECISIONS}
    for r in rows:
        counts[r["decision"]] = r["n"]
    return counts
