# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **The customer workspace.** `/portal` lists a customer's own tickets with three states —
  Handled by AI, A human agent is reviewing, Resolved — and `/portal/chat/<id>` carries the
  conversation. Raising a ticket runs the same pipeline the console runs; the turn moved
  into `app/pipeline.py` and takes the session it works on, so the portal can use a
  short-lived one per request instead of the console's process-wide global.
- **Customers can rate a resolved ticket** — five stars and an optional line, changeable
  for 24 hours. The storage and the dashboard tile already existed; what was missing was
  somewhere for the person with the opinion to answer from. Both routes call the same
  `record_csat()`, so the rules have one implementation.

### Changed

- **Three roles, three workspaces.** `lead` is retired and folded into `admin`; `customer`
  is new. A customer raises and tracks tickets in **/portal**, an agent works escalated
  cases in the console, and an admin adds the dashboard, the write-action gate and the
  exports. Accounts still on `lead` are moved to `admin` on boot rather than left holding a
  role that no longer exists.
- **A wrong turn sends you to your own workspace**, not to a 403 page. `/denied` is gone.
  `/portal` is guarded by role membership rather than rank, so an admin — who outranks a
  customer — is kept out of the customer's workspace just as firmly.
- **The console is the agent's alone.** The Customer / Agent toggle is gone; what you type
  is always an agent reply, and the composer says whose. The six Common Issues chips now
  fill the agent's reply box with an opening line instead of a customer's complaint.
- **Sign-up creates a customer, not an agent.** Somebody who finds the login page and makes
  an account is a person with a problem, not a member of staff.

### Added

- **Login by email, with a role selector and one-click demo accounts.** The chosen card is
  checked against the account rather than trusted — picking Admin does not make you one.


- **The work queue is in the console**, as a third column: search, All / Critical / High
  filters, click-to-open with no page reload, the open case shown selected, and `j` / `k` /
  `Enter` to move and open. It refreshes after every send and every resolve. The ordering is
  `work_queue()`'s — the same function the dashboard uses — served through a new
  `/api/queue` that an agent can reach, since `/api/stats` is lead-only.
  Three columns above 1100px, a drawer below it, and one column with a tab switcher below
  700px.

### Fixed

- **The console header scrolled the page sideways on a phone.** It had no layout below
  1020px at all; at 640px the row was 1278px wide. It now wraps, and the quick-pick strip
  scrolls within itself instead of dragging the document with it.

- **Customer satisfaction.** `csat_score`, `csat_comment` and `csat_at` on the case, with a
  rating API that accepts one rating per resolved case and keeps it editable for 24 hours.
  The dashboard gains a **CSAT** tile (average out of 5 with the response rate), an
  **Avg first reply** tile (median, labelled as such), a CSAT line on the scores chart
  against its own 1–5 axis, **Satisfaction by who resolved it** as three bars, and a
  **Volume by category** panel driven by intent and falling back to the matched help
  article. Any average over fewer than 10 ratings is shown faintly with its sample size
  rather than hidden or presented as settled.
  The collection UI is not built: it belongs on `/portal`, which does not exist yet.

- **Voice in both directions, on browser APIs only.** Dictation now shows interim words grey
  inside the message box and firms them up as they settle; every AI-composed reply gets a
  Play button backed by `speechSynthesis`, picking a voice by the message's own script so a
  Hindi reply is read by a Hindi voice. A **Voice** switch in the header puts every voice
  control away for presenting somewhere noisy, and a low-confidence transcript is flagged
  "check this transcript" rather than sent silently.
- **Messages record how they arrived** — dictated or typed — and the transcript shows a small
  mic on the dictated ones. Voice is an input method: the text goes down exactly the same
  pipeline either way.

### Changed

- **An unsupported browser now disables the mic instead of hiding it**, with a tooltip saying
  which browsers can do it. A control that vanishes reads as a bug; a disabled one with a
  reason reads as an answer.

- **Trends is interactive.** A 24h / 7d / 14d / 30d range toggle above the charts, remembered
  in `localStorage` and defaulting to 7 days; hover and touch tooltips carrying every series
  value plus a derived line; PNG and JSON export per chart. The 24-hour view buckets by hour,
  because a day split into days is one or two bars. Still hand-written SVG — the PNG export
  serialises the chart onto a canvas rather than shipping a rendering library.

- **Resolution mode on every case** — `ai_autonomous`, `hybrid` or `human`, derived from
  who composed each outgoing message rather than typed in anywhere. Messages now carry a
  `source`, set when they are sent; older cases were backfilled on first boot by the same
  rules, so the live value and the historical one mean the same thing.
- **Dashboard: who resolved what.** A three-way split under Resolution progress with counts
  and shares, "AI handled end to end" and "Human touch rate" beside it, a *Resolved by*
  column and filter on Cases, and a stacked area on Trends showing the three modes over
  time — hand-drawn SVG, matching the existing charts.

- **Write actions, behind a human gate.** `initiate_refund`, `expedite_delivery` and
  `reset_account_access` replace `issue_refund` and `send_password_reset`. The model can
  propose one; only a person clicking **Approve and run** executes one.
- **An Actions audit trail**, on the dashboard beside Cases, with the same search, filters
  and CSV export. Every proposal and every decision is recorded — including the refused
  ones — with the case, who proposed, who decided and when.

### Changed

- **`allow actions` now withholds the tools rather than declining to run them.** With the
  box unticked the write declarations are no longer sent to the Gemini API at all, so the
  model has no function to call.

### Fixed

- **Ticking `allow actions` used to let the model execute writes with no human step.**
  `gather_facts()` ran any write tool directly once `allow_writes` was set, so a refund
  could be issued without anyone clicking anything. The loop now records a proposal and
  never executes a write, whatever that flag says. The checkbox's tooltip and toast, which
  both said refunds "will now be carried out for real", described that behaviour accurately
  and have been rewritten.

- **The function-calling loop could never chain two lookups.** Function results were sent back
  with `role="tool"`, which the Gemini API rejects outright (`Role 'tool' is not supported`).
  Round 1 worked, so the feature looked healthy in the UI, but every round after it returned
  400 — and the failure was swallowed into a `(lookup unavailable)` note, so nothing reached the
  log. Results now go back as `role="user"`, and the coach can read a failed order and then look
  up that order's refund.
- **A malformed request was retried against every model.** A `400 INVALID_ARGUMENT` means the
  request is wrong, not the model, so the fallback was guaranteed to fail identically — it only
  doubled the latency and quota spent before giving up. These now fail fast.

### Added

- **Accounts and roles.** Every page and every API route now sits behind a login and a role —
  `agent` (console, own cases), `lead` (+ dashboard, + the write-action gate), `admin`
  (+ bulk exports). Until now anyone who could reach the port could read every saved customer
  transcript, which sat badly next to a product that masks personal data before it reaches the
  model. See [`app/auth.py`](app/auth.py).
- Passwords stored as scrypt hashes, a fifteen-minute lockout after five failed attempts, and
  `HttpOnly` / `SameSite=Lax` session cookies. The cookie signing key is generated once and kept
  in the database, so a restart no longer signs everyone out.
- Account management from the command line — `--list-users`, `--add-user`, `--passwd`,
  `--disable-user`, `--enable-user`. Accounts cannot be created through the web app on purpose.
- Cases record the agent who opened them (`owner`), and an agent can only open their own or an
  unclaimed one. Cases from before this change are unowned and claimed by whoever opens them.
- A sign-in page, a "signed in but wrong role" page, and a user chip with sign-out in both front
  ends. Controls a role cannot use are hidden rather than left to fail with a 403.
- 47 tests covering role boundaries, lockout, ownership and the post-login redirect.
- Regression tests for the function-calling loop, including an assertion that every turn carries
  a role the API accepts.

- **Token metering and cost per conversation.** Every Gemini call now reports its own token
  counts, which are attributed to a case, an agent and a step (analyse, look up, draft, score,
  embed) and priced. The dashboard gained a **Cost** section, and each case shows its own bill.
  Counting API calls was the wrong unit: analysing one short message and drafting a grounded
  reply are both "one call" and differ by an order of magnitude. See [`app/metering.py`](app/metering.py).
- **Daily caps and a per-agent rate limit.** `DAILY_TOKEN_CAP`, `DAILY_COST_CAP_INR` and
  `RATE_LIMIT_CALLS_PER_MIN` refuse new work with a 429 and a `Retry-After` *before* the model
  is called. Any of them set to `0` is switched off.
- 26 tests for metering, pricing, the ceilings and the engine's usage hook.

### Changed

- The engine gained a `USAGE_HOOK` (notebook cells 1, 17 and 28). It announces the token cost
  of each call and knows nothing about storage, pricing or budgets — metering policy stays in
  the app, not in the coaching logic.
- A new case is given its id *before* the first model call, so the opening turn of every
  conversation is billed to a case instead of to nothing.
- `/api/health` stays public so a container probe can reach it, but now tells an anonymous
  caller only that the process is up — the key path and model name need a login.
- The `cases` table gained an `owner` column, applied to an existing database by a guarded
  migration on startup.

---

## [0.1.0] — 2026-09-15

First public version.

### Added

- **Coaching engine** (`AICoach`, generated from the notebook) — conversation-aware sentiment,
  urgency and escalation-risk analysis; agent scoring against a published 1–10 rubric for tone,
  empathy and clarity; drafted replies grounded in the knowledge base.
- **Semantic knowledge base** using Gemini embeddings and cosine similarity, with the keyword
  matcher kept as a fallback. Threshold `0.58`, measured rather than guessed.
- **Back-office tools** via Gemini function calling — `check_order_status`,
  `check_refund_status`, `issue_refund`, `send_password_reset` against a mock `orders.json`.
  Write-capable tools are recorded as requested and wait for human approval.
- **PII redaction** of phone numbers, emails, order ids and card numbers before any text reaches
  Gemini, on both the chat and embeddings paths, restored in the drafted reply.
- **Live console** — quick-pick chips, voice input in English and Hindi, escalation timeline,
  agent scorecard, suggested reply with 👍/👎.
- **Dashboard** — work queue, deflection rate, SLA breaches, issues over time, agent performance,
  suggestion acceptance, FAQ coverage, and a "questions we cannot answer" panel.
- **Auto-resolution** of confident, low-risk knowledge-base matches.
- **SLA tracking** with targets by escalation risk (15 min / 1 h / 4 h).
- **SQLite case store** with a migration from the previous `cases.json` and JSON/CSV export.
- **Test suite** that runs with no API key, and CI covering tests, lint, notebook/engine sync and
  a secret scan.

### The finding

Documented in the notebook: a standard multilingual sentiment model reads
`"bahut ganda service hai"` — plainly negative Hinglish — as **POSITIVE, 49.8% confidence**,
while the Gemini-backed analyser reads it as `negative` with frustration 85/100. Step 13
(English) escalates `35 → 35 → 43 → 94` and fires red; Step 17 (Hinglish) stays green throughout.
The escalation tracker was correct. The signal feeding it was not.

[Unreleased]: https://github.com/AmanKumar-23/support-coach/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/AmanKumar-23/support-coach/releases/tag/v0.1.0
