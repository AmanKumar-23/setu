# Architecture

How the pieces fit, and why they are arranged this way.

## Diagrams

| Figure | What it shows |
|---|---|
| [**Setu at a glance**](architecture/fig0-overview.png) | The whole system drawn as a bridge: customer on one bank, support team on the other, Gemini above, SQLite beneath |
| [**Containers**](architecture/fig1-containers.png) | Every browser page, route group, service and AI function, and the one HTTP boundary and one model client every call crosses |
| [**One message, end to end**](architecture/fig2-message-lifecycle.png) | The 13 steps of a customer turn, and which of them the customer actually waits for |
| [**Who answers**](architecture/fig3-who-answers.png) | The decision flow that picks the assistant or a person, with the severity scale |
| [**Guarantees**](architecture/table1-guarantees.png) | Ten guarantees and the function that enforces each |

Vector sources sit beside each PNG, and [`architecture/setu-architecture.html`](architecture/setu-architecture.html)
is the whole sheet as one page.

---

## The notebook is the source of truth

Most projects would treat a notebook as a scratchpad and the application as the real code. This
one is the other way round, on purpose.

```
customer_support_coach.ipynb        37 cells: the engine AND the written analysis
            │
            │   python3 app/build_core.py
            ▼
app/coach_core.py                   generated — never edited by hand
            │
            │   import
            ▼
app/server.py                       Flask: routes, SLA, analytics, storage
            │
            │   every route guarded by
            ▼
app/auth.py                         accounts, roles, sessions
```

The notebook carries the reasoning as well as the code: the Hinglish comparison, the measured
similarity thresholds, the before/after runs that justify each design decision. Those outputs are
the evidence for the project's central claim, so they are committed with the notebook rather than
stripped.

If the engine lived only in `app/`, the notebook would drift into a stale copy within a week —
and a reader could no longer trust that the printed results came from the code that ships.

### What `build_core.py` extracts

| Slice | Notebook cell | Contents |
|---|---|---|
| Key helper | 1 | `get_gemini_api_key()`, `find_key_file()`, key-file search |
| Knowledge | 17 | `KNOWLEDGE_BASE`, embeddings, cosine similarity, `find_kb_article()` |
| Data models | 26 | `Message`, `CoachingFeedback`, `ConversationState` |
| Engine | 28 | Response schemas, `Redactor`, back-office tools, `AICoach` |

Imports are read out of the notebook rather than hardcoded, after a stale hardcoded banner once
dropped `import time` and produced a `NameError` that only appeared when a retry fired. A
`symtable` pass now warns about any global the generated module references but never defines.

**CI enforces this.** The `notebook-sync` job regenerates `coach_core.py` and fails if the result
differs from the committed file.

---

## Request flow

A customer message arriving at `POST /api/portal/cases/<id>/message`. The customer waits for
one database write; everything that touches the model happens after the request has returned.

```
  browser                                   the customer waits for this part only
     │  { "text": "mera recharge fail ho gaya" }
     ▼
  pipeline.record_customer_message()        save the message, mark ai_pending   ≈ 6 ms
     │  200 ──► the chat shows "Setu is typing"
     │
     └─► AI_POOL.submit(case)               ThreadPoolExecutor × 4, one worker per case
            │
            ├─► 1. knowledge-gap check      embeddings; a miss is logged, never escalated
            ├─► 2. analyze_customer_message(msg, history)
            │         redact ─► Gemini (JSON schema) ─► sentiment, urgency, escalation risk,
            │         frustration 0–100, trend, key issue, intent + confidence, emotion
            ├─► 3. severity_of()            Low · Medium · High · Critical, with the rules that fired
            ├─► 4. gather_facts()           read-only look-ups by function calling (skipped past 25 s)
            ├─► 5. handover_reason()        High/Critical, a cancellation, a billing dispute or a
            │                               proposed write ──► a person; else the assistant answers
            ├─► 6. try_auto_resolve()       calm case + article match ≥ 0.65 ──► answer and close
            ├─► 7. suggest_reply() / handoff_message()   in the customer's language
            └─► 8. commit under the case lock           re-reads the case, keeps anything new
```

The console runs the same `respond_to_customer()` synchronously. There is one pipeline and two
callers, not two implementations.

**Language.** The reply language is detected per message — script ranges, Hindi/Marathi marker
words, a Hinglish word list — so a customer who switches mid-conversation is answered in what
they just used. Analysis stays in English, so the queue, the dashboard and the knowledge grouping
read one vocabulary.

**Write actions.** `allow_writes` decides which tool *declarations* are sent. Customers' turns
never carry the write tools; on an agent's turn a call to one becomes a **proposal**, and only
`POST /api/actions/<id>/decide` — an admin's click — ever runs it.

### The redaction boundary

```
   customer text ──► Redactor.redact() ──► Gemini ──► Redactor.restore() ──► agent
                          │                                  ▲
                     placeholders                       real values
                   ORDER_1, PHONE_1 …               put back locally
```

A fresh `Redactor` is created per call, so one customer's placeholders can never resolve to
another's details and the map cannot grow without bound in a long-running server. The map lives
only in this process; the real values are never sent.

Function *arguments* come back from the model holding placeholders, because placeholders are all
it was ever given. They are restored before the function runs — the lookup happens inside our own
process, where the real values were never a secret — and the *result* is redacted again on the
way back to the model.

---

## Resilience

`_call_model` is the single door to Gemini. Every path goes through it — plain text, structured
JSON and function calling alike.

| Failure | Response |
|---|---|
| `503`, `429`, `500`, `UNAVAILABLE`, `RESOURCE_EXHAUSTED` | Retry with backoff (1 s, 2 s, 4 s), then fall back to the other model |
| A dropped or timed-out connection | Retried like a busy server, rather than surfaced as a raw exception |
| `400`, `401`, `403`, `INVALID_ARGUMENT` | Fail immediately — the request is malformed, so every other model rejects it identically |
| One request runs long | Abandoned at **20 s** and retried |
| The whole call runs long | Stops at **45 s**; no attempt starts past the ceiling |
| A slow turn | Skips the order look-up after 25 s and the bespoke handoff note after 35 s |
| The model is unreachable | The case goes to a person with a "we have your message" note in the customer's language; the full error goes to the `setu` log under a short reference |
| A look-up fails | The reply is written without it; the analysis is kept |

Function calling has a tighter free-tier quota than ordinary generation, which is why it needs
this more than the other paths do.

---

## Storage

SQLite, six tables. `cases` keeps indexed scalar columns for the queries the dashboard actually
runs and the full case — transcript, per-message analysis, timeline, redaction log, scores — as a
JSON blob beside them.

| Table | Holds |
|---|---|
| `cases` | `id`, `status`, `escalation_risk`, `sentiment`, `opened_at`, `closed_at`, `owner`, `customer`, `subject`, `category`, `updated_at`, and the case as JSON |
| `users` | Accounts, roles, scrypt hashes, lockout counters |
| `settings` | The session signing key and other one-off values |
| `actions` | Every proposed write action and who approved or refused it |
| `usage` | Tokens and estimated cost per model call, by case, user and step |
| `faq_misses` | Questions the knowledge base could not answer |

Columns added after the first release are applied by `ALTER TABLE` on start, so an older database
upgrades in place. `migrate_from_json()` imports the original `cases.json` on first run; JSON and
CSV export still work.

---

## The front end

Vanilla JavaScript and inline SVG — no framework, no build step, no `node_modules`.

| File | What it is |
|---|---|
| `app/static/login.html` | Sign in by email or username, role cards, sign-up for customers, one-click demo accounts |
| `app/static/portal.html` | The customer's tickets, issue tags and FAQ card |
| `app/static/portal-chat.html` | One conversation: typing indicator, dictation, Play, language switching in place |
| `app/static/profile.html` | The customer's details and password |
| `app/static/index.html` | The agent console: escalated queue, conversation, live intelligence, scorecard |
| `app/static/dashboard.html` | Eight sections (Overview, Work queue, Breaching soon, Cases, Trends, Knowledge, Actions, Cost) |
| `app/static/i18n.js` | The UI catalogue: English and a hand-written Hindi seed; other languages translated once and cached |
| `app/static/voice.js` | Browser speech recognition and synthesis, shared by the portal and the console |

Charts are drawn as inline SVG in the page's own style rather than pulled from a charting
library, so every page is one file a contributor can read end to end.

---

## Access control

Every page and every API route is behind a login and a role. `app/auth.py` holds it; `server.py`
only decorates.

### Roles are ranked, not enumerated

```
customer (0)      agent (1)  ──►  admin (2)
   │                                  
   └── its own workspace, off the staff ladder entirely
```

Among STAFF, each role is a superset of the one below, so a guard asks "at least `agent`?"
rather than "in this set of roles?". A set would let someone hold `admin` without holding
`agent`, and every check would have to remember to list both. Ranking makes that gap
unrepresentable.

A customer is **not** the bottom of that ladder. They are a different audience who must be kept
out of the console, not let in with fewer buttons. Rank 0 is what makes every staff guard
exclude them without one of those guards being edited — and `/portal` is guarded by
`require_exact("customer")` rather than a floor, because a floor would let an admin, who
outranks a customer, wander into the customer's workspace.

The whole policy is one dict in `server.py` — sixteen routes, each with the role it needs —
rather than a decorator argument scattered down nine hundred lines. It can be read, and audited,
in one screen.

### Two decisions worth explaining

**A case an agent does not own answers `404`, not `403`.** Telling somebody a case exists but is
not theirs still tells them it exists, along with how many there are and roughly when they were
opened. The dashboard roles see everything, so nothing is hidden from the people who need it.

**Accounts cannot be created through the web app.** They are a command-line operation. An
application that can mint its own admin is one request-handling bug away from having no roles at
all, and nothing in this product needs self-service sign-up.

### What the login does not cover

Session cookies are `HttpOnly` and `SameSite=Lax`. Lax is what stops a cross-site POST carrying
the cookie, which is the practical CSRF defence here — but it is not the same as per-request CSRF
tokens, and those are the next thing to add.

---

## Metering

Every Gemini call funnels through `AICoach._call_model()`. That is not a
coincidence -- it is the reason the retry and fallback logic works -- and it
makes it the one place token usage can be read from with no way for a new kind
of call to be added later and quietly escape it.

```
AICoach._call_model()  ──►  coach_core.report_usage()  ──►  USAGE_HOOK
                                                              │
                                              app/metering.py │  price it,
                                                              ▼  store it
                                                        usage table
```

The engine **announces**; it does not decide. `report_usage()` reads the token
counts off the response and hands them to whatever `USAGE_HOOK` is set to. The
notebook leaves it `None` and nothing happens; the app points it at
`metering.record`. So pricing, budgets and storage never end up inside the
coaching logic, and the notebook keeps running unchanged.

### Measured versus derived

Tokens come from the API's own `usage_metadata`. They are exact, and they are
the number to trust. Rupees are those tokens multiplied by a rate that is
**configuration** -- prices change, and the shipped defaults are placeholders.
The two are kept visibly separate: the dashboard labels every rupee figure
"estimated rates" until the rates are set explicitly.

### Attribution without threading a parameter

A request opens a billing account in a `ContextVar` before doing anything, and
names the case as soon as it knows which one it is. `record()` reads that
account at the moment a call completes. A `ContextVar` rather than a global
because Flask serves requests on many threads and two agents mid-conversation
must not be billed to each other -- and a mutable account rather than a fixed
pair because a brand new conversation has no id until part way through.

### Where the ceilings sit

The caps are checked at the **start of a turn**, in the route, not before each
individual call. Aborting half way through a tool-calling loop would leave the
customer with a reply quoting a lookup that never finished. One turn may
overshoot slightly; the next is refused with a 429.

---

## Deliberate limits

This is a coursework and demonstration project, and it is worth being explicit about what it is
not:

- **One process.** The portal builds a short-lived session per request, but the console still
  keeps one live session per process, and the per-case locks and the worker queue live in
  memory. It does not scale out across machines as it stands.
- **No per-request CSRF tokens.** `SameSite=Lax` cookies cover the realistic attack; tokens
  cover the rest.
- **The shipped token prices are placeholders.** Token counts are measured and correct; the
  rupee figures are only as good as the rates in `app/metering.py`, and the UI says so until
  they are set.
- **The caps are per process.** They read a shared SQLite table, so several workers on one
  machine stay consistent, but nothing coordinates two machines.
- **The back office is a mock.** `orders.json` is a file, not an order management system.
- **Thresholds come from a small sample.** `0.58` and `0.65` were measured, and the measurements
  are in the notebook, but the sample is small enough that they should be re-measured before
  anyone relies on them.
- **One language pair measured.** Replies work in ten languages plus Hinglish, but the
  sentiment finding was measured on Hinglish only; whether it repeats in Tamil, Bengali or
  Marathi is untested, and measuring it would be a genuine contribution.
- **Street addresses are not redacted.** Email, card numbers, order ids and phone numbers are.
