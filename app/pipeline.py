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
_handoff_fallback = None
_record_proposals = None


def configure(now_iso, record_proposals=None, handoff_fallback=None):
    global _now_iso, _record_proposals, _handoff_fallback
    _now_iso = now_iso
    _record_proposals = record_proposals
    # (language) -> a ready-written handoff note, for when the model cannot
    # write one. Lives in the server, where the translated catalogue is.
    _handoff_fallback = handoff_fallback


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
# Severity
#
# One four-level scale for the whole app -- low, medium, high, critical --
# and it measures how BADLY this is going, not how happy the customer is.
# High and critical are the ones a person has to see. (The brief suggested
# "low = negative, high = positive" for the chat; on that scale "escalate
# high" would escalate the happiest customers, so the two had to be one
# scale, pointing one way.)
#
# Computed here from the model's own readings rather than asked of the model
# as one more word, so every level comes with the rule that produced it and
# an agent can see WHY a case was escalated, not just that it was.
# --------------------------------------------------------------------------
SEVERITY_LEVELS = ("low", "medium", "high", "critical")

# Frustration at or above these is high / critical on its own.
HIGH_FROM = 70
CRITICAL_FROM = 85

# Said outright, these are critical whatever the score: the customer has
# stopped asking and started threatening.
THREATS = ("consumer court", "consumer forum", "legal action", "lawyer",
           "police", "ombudsman", "sue you", "social media", "twitter",
           "going viral", "fraud complaint", "cyber cell")


def severity_of(analysis, text=""):
    """(level, score, reasons) for one customer message."""
    score = analysis.get("frustration")
    try:
        score = max(0, min(100, int(score)))
    except (TypeError, ValueError):
        score = FALLBACK_SCORE.get(analysis.get("escalation_risk"), 35)

    risk = analysis.get("escalation_risk")
    emotion = analysis.get("emotion")
    intent = analysis.get("intent")
    sentiment = analysis.get("sentiment")
    urgency = analysis.get("urgency")
    lowered = str(text or "").lower()

    rules = {
        "critical": [
            (score >= CRITICAL_FROM, f"frustration {score}/100"),
            (any(t in lowered for t in THREATS),
             "threatens: " + ", ".join(t for t in THREATS if t in lowered)),
            (emotion == "Angry" and risk == "high", "angry, and high escalation risk"),
        ],
        # Calibrated against the live model: it rates an ordinary first
        # "recharge failed, money taken" at about 65 frustration, negative and
        # urgent -- which is most support tickets. With the line at 65 and
        # "urgent and unhappy" counted as high, 58% of real messages were
        # handed to a person, including the routine ones the assistant is best
        # equipped to answer. High now needs a real sign of trouble.
        "high": [
            (score >= HIGH_FROM, f"frustration {score}/100"),
            (risk == "high", "high escalation risk"),
            (emotion == "Angry", "angry"),
            (intent in ("Cancellation", "Complaint"), f"wants: {intent}"),
        ],
        "medium": [
            (urgency == "high" and sentiment == "negative", "urgent and unhappy"),
            (score >= 35, f"frustration {score}/100"),
            (sentiment == "negative", "negative"),
            (emotion in ("Frustrated", "Anxious"), (emotion or "").lower()),
            (risk == "medium", "some escalation risk"),
        ],
    }
    for level in ("critical", "high", "medium"):
        reasons = [why for fired, why in rules[level] if fired]
        if reasons:
            return level, score, reasons
    return "low", score, ["calm; nothing urgent"]


# Intents that need a person to DO something the assistant cannot: cancel an
# account, reverse a disputed charge. Customers are never given the write
# tools, so the assistant could only promise these -- and "no write action
# ever executes automatically" means it must not. A refund STATUS is a
# lookup, and stays with the assistant.
NEEDS_A_PERSON = ("Cancellation", "Billing dispute")

# A budget for the whole turn, in seconds since it began. Each model call is
# bounded on its own, but four bounded calls in a row on a bad day added up to
# two minutes of "typing" -- measured, with Gemini returning 504s. So the two
# steps the reply can do without are skipped once the turn is already slow:
#   the order lookup   -- the reply is written without live order data
#   a bespoke handoff  -- the translated template is used instead
# The analysis and the reply itself always run: without them there is no turn.
LOOKUP_ONLY_UNDER_S = 25
BESPOKE_HANDOFF_ONLY_UNDER_S = 35


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


def apply_analysis(sess, analysis, text, *, reply_lang=None, how=None):
    """Put one reading of `text` onto the case AND onto the message itself.

    Shared by the live turn and by opening a case that was never analysed
    (the model was down when it arrived), so the two cannot drift apart.
    Returns (level, score, reasons).
    """
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

    level, score, reasons = severity_of(analysis, text)
    sess.severity = {"level": level, "score": score, "reasons": reasons}

    if reply_lang is None:
        reply_lang, how = languages.reply_language(
            text, previous=getattr(sess, "language", None))

    # The reading belongs to THIS message as well as to the case, so the
    # history can be read back turn by turn -- and so a later, calmer message
    # does not erase the record of the one that was not.
    reading = {
        "severity": level, "severity_score": score, "severity_reasons": reasons,
        "sentiment": state.sentiment, "urgency": state.urgency,
        "intent": state.intent, "intent_confidence": state.intent_confidence,
        "emotion": state.emotion, "language": reply_lang, "language_how": how,
    }
    for message in reversed(state.history):
        if message.speaker == "customer" and message.text == text:
            message.analysis = reading
            detected = languages.detect_message_language(text)
            message.language = (reply_lang if detected == "devanagari"
                                else detected or "")
            break
    return level, score, reasons


def respond_to_customer(sess, text, *, language=None, commit=_persist):
    """Everything the model does for one customer message.

    Analyse, look up, decide, and -- on a case the assistant is handling --
    answer the customer directly, in the language they just wrote in.
    `commit(sess, **persist_kwargs)` does the final save; the background
    worker passes one that re-reads the case first, so a message the customer
    sent while this was running is kept rather than overwritten.

    `language` is the customer's CHOSEN language, the fallback for a message
    that gives nothing to detect ("ok", "499"). The reply language itself is
    detected from THIS message, so a customer who switches mid-conversation
    is answered in whatever they just used.

    Analysis, lookups and scoring stay in English however the customer
    writes: the dashboard, the work queue and the knowledge grouping all read
    one vocabulary. Only the reply is in the customer's language.

    Raises whatever the model raised if the ANALYSE step fails. Every later
    step is best-effort: a failed lookup or a failed draft must not cost the
    turn its analysis.
    """
    preferred = languages.normalise(language) if language else None
    reply_lang, how = languages.reply_language(
        text, previous=getattr(sess, "language", None), preferred=preferred)
    sess.language = reply_lang
    timings = {}
    turn_began = time.perf_counter()

    def spent():
        return time.perf_counter() - turn_began

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
    level, score, reasons = apply_analysis(sess, analysis, text,
                                           reply_lang=reply_lang, how=how)

    # Look up now rather than waiting for the agent to reply, so the facts are
    # on screen while they are still typing.
    if spent() < LOOKUP_ONLY_UNDER_S:
        try:
            sess.facts = timed("lookup", sess.coach.gather_facts,
                               text, state.history,
                               allow_writes=sess.allow_writes)
        except Exception:
            sess.facts = []      # a failed lookup must not lose the turn
    else:
        sess.facts = []          # already slow: answer without it
        timings["lookup_skipped"] = int(spent() * 1000)

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

    for_the_model = {"sentiment": state.sentiment, "urgency": state.urgency,
                     "key_issue": state.key_issue, "emotion": state.emotion,
                     "intent": state.intent,
                     "intent_confidence": state.intent_confidence,
                     "severity": level}
    note = languages.reply_instruction(reply_lang)

    sess.auto_reply = None
    sess.ai_pending = False
    sess.last_timings = timings

    # ---- who answers ----
    # The assistant speaks to the customer only on a case it is handling. A
    # case a person has taken -- or an older one, from before the assistant
    # answered anyone -- gets a draft for that person instead.
    why_a_person = handover_reason(sess, analysis, level, reasons)
    handed_over = sess.handler == "ai" and bool(why_a_person)
    if handed_over:
        sess.handler = "human"
        sess.handover = {"at": _now_iso(), "why": why_a_person,
                         "severity": level, "score": score}
        # Said ONCE, at the moment a person takes over: their issue is heard,
        # and someone is coming. In their language, and without a word about
        # severity, queues or scores.
        note_to_customer = ""
        if spent() < BESPOKE_HANDOFF_ONLY_UNDER_S:
            try:
                note_to_customer = timed(
                    "handoff", sess.coach.handoff_message, text, state.history,
                    key_issue=state.key_issue, language_note=note)
            except Exception:
                note_to_customer = ""
        else:
            timings["handoff_skipped"] = int(spent() * 1000)
        if not note_to_customer and _handoff_fallback is not None:
            note_to_customer = _handoff_fallback(reply_lang)
        if note_to_customer:
            # A Setu notice, not a reply: it does not stop the first-response
            # clock or count as anyone resolving the case -- the person who
            # answers next does both.
            state.add_message("agent", note_to_customer, source="system",
                              language=reply_lang)
    assistant_answers = sess.handler == "ai"

    auto = None
    if assistant_answers:
        try:
            auto = timed("decide", try_auto_resolve, sess, text, reply_lang)
        except Exception:
            auto = None          # never let this break an ordinary turn

    if auto:
        # A help article matched strongly on a calm conversation: answered,
        # and closed, as before.
        sess.auto_reply = auto
        state.add_message("agent", auto["reply"], source="ai",
                          language=reply_lang)
        open_rating_slot(sess, True, auto.get("topic"))
        sess.last_suggestion = auto["reply"]
        if sess.first_response_at is None:
            sess.first_response_at = _now_iso()
        commit(sess, status="auto_resolved", closed_at=_now_iso())
    else:
        try:
            reply = timed("reply", sess.coach.suggest_reply,
                          text, state.history, analysis=for_the_model,
                          facts=sess.facts, language_note=note)
            note_redactions(sess)
            article = getattr(sess.coach, "last_article", None)
            open_rating_slot(sess, article is not None,
                             article.get("topic") if article else None)
        except Exception:
            reply = ""           # a failed draft must not cost the analysis

        sess.last_suggestion = reply
        if assistant_answers and reply:
            # The assistant's answer, sent. It stays open: a reply is not a
            # resolution, and the customer may well write back.
            state.add_message("agent", reply, source="ai", language=reply_lang)
            if sess.first_response_at is None:
                sess.first_response_at = _now_iso()
        commit(sess, reopen=True)

    result = TurnResult(analysis, elapsed, sess.facts,
                        sess.auto_reply, sess.last_suggestion)
    result.timings = timings
    result.severity = sess.severity
    result.reply_language = reply_lang
    return result


def handover_reason(sess, analysis, level="low", reasons=()):
    """Why a person has to take this case, or "" if the assistant can.

    High and critical severity: the customer is angry, threatening, or has
    been let down enough that a person should be the one to answer.

    And the standing rule: nothing that moves money or closes an account
    happens without a person. The assistant has no write tools on a
    customer's turn, so all it could do with one of these is promise it --
    which it must not.
    """
    if level in ("high", "critical"):
        return f"severity {level}: " + "; ".join(reasons)
    if any(f.get("proposed") for f in (sess.facts or [])):
        return "a write action was proposed"
    if analysis.get("intent") in NEEDS_A_PERSON:
        return f"{analysis['intent']} needs a person"
    return ""


def run_customer_turn(sess, text, *, channel="typed", language=None):
    """Record and respond in one go -- the console's synchronous path.

    The portal does NOT use this: it records in the request and responds in
    the background, so a customer never waits on the model.
    """
    record_customer_message(sess, text, channel=channel)
    return respond_to_customer(sess, text, language=language)
