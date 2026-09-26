"""The one path a customer message takes, whoever sent it.

The agent console and the customer portal must not be two implementations of
"analyse, look up, then either answer or draft". They are two CALLERS of this
one, differing only in which session object they hand it.

Why a session is passed in rather than read from a global: the console owns a
single module-level LiveSession -- one conversation at a time, process-wide --
which is fine for one agent at one keyboard and catastrophic the moment a
customer is typing at the same time. Her message would land in his transcript.
The portal therefore builds a SHORT-LIVED session per request, loads the case
into it, runs the turn, persists, and throws it away.

That works because load() and persist() already bracket every turn: the
database is the source of truth and the session is only ever a scratchpad
between those two calls.

Nothing here imports Flask. The caller owns HTTP.
"""

import time

import coach_core
import languages

# Where a risk band lands when the model gave no number of its own.
FALLBACK_SCORE = {"low": 25, "medium": 55, "high": 90}

# A semantic match at or above this answers the customer outright. A keyword
# match never qualifies however high it scores: its number is a word count,
# not a confidence.
AUTO_RESOLVE_THRESHOLD = 0.65

# Wired by the server, which owns the clock and the audit trail. Same shape
# as auth.configure() and metering.configure() -- one place that knows.
_now_iso = None
_record_proposals = None


def configure(now_iso, record_proposals=None):
    global _now_iso, _record_proposals
    _now_iso = now_iso
    _record_proposals = record_proposals


class TurnResult:
    """What one customer turn produced. Plain data; the caller serialises."""

    def __init__(self, analysis, latency_ms, facts, auto_reply, suggestion):
        self.analysis = analysis
        self.latency_ms = latency_ms
        self.facts = facts
        self.auto_reply = auto_reply
        self.suggestion = suggestion

    @property
    def answered_outright(self):
        return self.auto_reply is not None


# --------------------------------------------------------------------------
# Helpers, now taking the session they work on
# --------------------------------------------------------------------------
def is_knowledge_gap(text):
    """True when nothing we have answers this. Feeds "Questions we cannot
    answer" -- a gap is a help article somebody still has to write."""
    try:
        return coach_core.knowledge_gap_detector(text)
    except Exception:
        return False          # without Part 1's KB we cannot judge


def note_redactions(sess):
    """Fold the last call's redaction log into the case, without repeats."""
    seen = {(r["kind"], r["placeholder"], r["masked"]) for r in sess.redactions}
    for entry in getattr(sess.coach, "last_redactions", []) or []:
        key = (entry["kind"], entry["placeholder"], entry["masked"])
        if key not in seen:
            seen.add(key)
            sess.redactions.append(entry)


def open_rating_slot(sess, grounded, topic=None):
    """Record that a suggestion was produced, ready for a thumbs up or down."""
    sess.ratings.append({"rating": None, "grounded": bool(grounded),
                         "topic": topic, "at": _now_iso()})


def try_auto_resolve(sess, text, language="en"):
    """Answer outright when the conversation is calm AND an article matched."""
    if sess.state.escalation_risk != "low":
        return None

    article = coach_core.find_kb_article(text)
    if not article or article.get("how") != "semantic":
        return None
    if article.get("score", 0) < AUTO_RESOLVE_THRESHOLD:
        return None

    reply = sess.coach.suggest_reply(
        text, sess.state.history,
        analysis={"sentiment": sess.state.sentiment,
                  "urgency": sess.state.urgency,
                  "key_issue": sess.state.key_issue},
        facts=sess.facts,
        language_note=languages.reply_instruction(language),
    )
    return {"reply": reply, "topic": article["topic"],
            "confidence": article["score"]}


# --------------------------------------------------------------------------
# The turn
# --------------------------------------------------------------------------
def record_customer_message(sess, text, *, channel="typed"):
    """The part of a turn the customer actually waits for. No model calls.

    Saves their message and marks the case as waiting on the assistant. The
    request that called this can answer in milliseconds; everything the model
    does happens afterwards, in respond_to_customer().
    """
    sess.state.add_message("customer", text, channel=channel)
    sess.last_customer_message = text
    # NOT the knowledge-gap check. It sounds like bookkeeping but it runs an
    # embedding search -- 0.5s warm, 1.8s cold -- and it was the last model
    # call left holding the request up. It runs first thing in the worker.
    sess.ai_pending = True
    # Also gives a new case its id, before any model call is billed to it.
    sess.persist(reopen=True)


def _persist(sess, **kwargs):
    sess.persist(**kwargs)


def respond_to_customer(sess, text, *, language=None, commit=_persist):
    """Everything the model does for one customer message.

    Analyse, look up, then either answer outright or draft for an agent.
    `commit(sess, **persist_kwargs)` does the final save; the background
    worker passes one that re-reads the case first, so a message the customer
    sent while this was running is kept rather than overwritten.

    `language` is the language the REPLY is written in. It reaches
    suggest_reply and nothing else: analyse, lookup and score stay in English
    however the customer writes, because the dashboard, the work queue and
    the knowledge grouping all read one vocabulary.

    Raises whatever the model raised if the ANALYSE step fails. Every later
    step is best-effort: a failed lookup or a failed draft must not cost the
    turn its analysis.
    """
    language = languages.normalise(language or getattr(sess, "language", None))
    sess.language = language
    timings = {}

    def timed(step, fn, *args, **kwargs):
        began = time.perf_counter()
        try:
            return fn(*args, **kwargs)
        finally:
            timings[step] = int((time.perf_counter() - began) * 1000)

    # Logged before the analysis: whether we can answer this has nothing to
    # do with whether the analysis succeeds.
    try:
        if timed("gap", is_knowledge_gap, text):
            sess.unanswered.append({"text": text, "at": _now_iso()})
    except Exception:
        pass                     # a failed search must not cost the turn

    # The whole conversation, so the model judges the trajectory rather
    # than one isolated sentence.
    analysis = timed("analyse", sess.coach.analyze_customer_message,
                     text, sess.state.history)
    elapsed = timings["analyse"]

    state = sess.state
    state.sentiment = analysis.get("sentiment", "unknown")
    state.urgency = analysis.get("urgency", "unknown")
    state.escalation_risk = analysis.get("escalation_risk", "unknown")
    state.key_issue = analysis.get("key_issue", "")
    state.frustration = analysis.get(
        "frustration", FALLBACK_SCORE.get(state.escalation_risk, 35))
    state.trend = analysis.get("trend", "flat")
    # analyze_customer_message() has already forced these into their enums.
    state.intent = analysis.get("intent", "")
    state.intent_confidence = analysis.get("intent_confidence")
    state.emotion = analysis.get("emotion", "")
    note_redactions(sess)
    sess.trajectory.append(state.frustration)

    # Look up now rather than waiting for the agent to reply, so the facts are
    # on screen while they are still typing.
    try:
        sess.facts = timed("lookup", sess.coach.gather_facts,
                           text, state.history, allow_writes=sess.allow_writes)
    except Exception:
        sess.facts = []          # a failed lookup must not lose the turn

    # Anything the model asked to WRITE is recorded as a proposal. It is not
    # run here, and there is no branch below that runs it.
    if _record_proposals is not None:
        try:
            _record_proposals(sess)
        except Exception as error:
            # Fails closed and loudly: with no row in the trail there is no
            # card to approve, so nothing can run -- but a silent audit
            # failure on a safety path is its own bug.
            print(f"  AUDIT: could not record proposals: {error}")

    sess.auto_reply = None
    try:
        auto = timed("decide", try_auto_resolve, sess, text, language)
    except Exception:
        auto = None              # never let this break an ordinary turn

    sess.ai_pending = False
    sess.last_timings = timings

    if auto:
        sess.auto_reply = auto
        # A real reply, so it goes in the transcript and stops the
        # first-response clock. Answering instantly is rather the point.
        state.add_message("agent", auto["reply"], source="ai")
        open_rating_slot(sess, True, auto.get("topic"))
        sess.last_suggestion = auto["reply"]
        if sess.first_response_at is None:
            sess.first_response_at = _now_iso()
        commit(sess, status="auto_resolved", closed_at=_now_iso())
    else:
        # Draft NOW, on the customer's turn. Waiting until the agent has
        # typed something is backwards: by then they have done the work the
        # draft was meant to save.
        try:
            sess.last_suggestion = timed(
                "draft", sess.coach.suggest_reply,
                text, state.history,
                analysis={"sentiment": state.sentiment,
                          "urgency": state.urgency,
                          "key_issue": state.key_issue},
                facts=sess.facts,
                language_note=languages.reply_instruction(language),
            )
            note_redactions(sess)
            article = getattr(sess.coach, "last_article", None)
            open_rating_slot(sess, article is not None,
                             article.get("topic") if article else None)
        except Exception:
            # A failed draft must not cost the analysis or the lookups.
            sess.last_suggestion = ""

        commit(sess, reopen=True)

    result = TurnResult(analysis, elapsed, sess.facts,
                        sess.auto_reply, sess.last_suggestion)
    result.timings = timings
    return result


def run_customer_turn(sess, text, *, channel="typed", language=None):
    """Record and respond in one go -- the console's synchronous path.

    The portal does NOT use this: it records in the request and responds in
    the background, so a customer never waits on the model.
    """
    record_customer_message(sess, text, channel=channel)
    return respond_to_customer(sess, text, language=language)
