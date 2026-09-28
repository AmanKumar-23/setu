# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.0] — 2026-09-28

**Setu.** The coaching console grows into a support desk with three workspaces: customers are
answered directly, in their own language, and handed to a person the moment a machine should not
be the one deciding.

### Added

- **The customer workspace.** `/portal` lists a customer's own tickets — Handled by AI, A human
  agent is reviewing, Resolved — and `/portal/chat/<id>` carries the conversation. Fourteen issue
  tags open a real ticket in one click, an FAQ card answers common questions before they become
  tickets, a profile page shows the account and changes what is safe to change, and a resolved
  ticket can be rated with five stars and a line, editable for 24 hours.
- **Replies in ten languages and Hinglish.** The reply language is detected per message — script
  ranges, Hindi/Marathi marker words, a Hinglish word list — so a customer who switches mid-chat
  is answered in what they just used. Analysis stays in English, so the queue and the dashboard
  read one vocabulary. See [`app/languages.py`](app/languages.py).
- **The whole portal UI switches language without a reload.** English and a hand-written Hindi
  catalogue ship with the app; other languages are translated once by the model, metered as their
  own step, and cached.
- **Voice in both directions, on browser APIs only.** Dictation shows interim words as they
  settle and never sends on its own; every AI reply has a Play button that picks a voice by the
  message's script, and a low-confidence transcript is flagged rather than sent silently.
- **An English gloss** under any customer message written in another script, in the console.
- **Four-level severity.** Low · Medium · High · Critical, computed from the model's readings —
  frustration 0–100, escalation risk, emotion, intent, urgency — plus threat words such as
  "consumer court" and "police". Each level records the rule that produced it.
- **Escalation to a person.** High and Critical severity, cancellations, billing disputes and any
  proposed write hand the case to a human, with a handoff note in the customer's language. The
  work queue is ordered by severity, then by the longest wait.
- **Intent and emotion extraction** — eleven intents with a confidence, six emotions, and the
  reading kept against each message so a conversation can be read back turn by turn.
- **Write actions behind a human gate.** `initiate_refund`, `expedite_delivery` and
  `reset_account_access` can only be proposed; an admin's **Approve and run** is the one place a
  write executes. An **Actions** audit trail records every proposal and decision.
- **Three roles, three workspaces** — `customer`, `agent`, `admin` — with sign-in by email or
  username, role cards, one-click demo accounts and customer sign-up. Passwords are scrypt hashes,
  five wrong attempts lock an account for fifteen minutes, and sessions last twelve hours.
- **The work queue inside the console** — search, severity filters, keyboard navigation, and the
  open case shown selected.
- **Dashboard: who resolved what** (AI autonomous · hybrid · human), CSAT and median first-reply
  tiles, volume by category, and an interactive Trends section with 24 h / 7 d / 14 d / 30 d
  ranges and PNG and JSON export per chart.
- **Token metering and cost per conversation**, attributed to a case, an agent and a step, with
  daily token and spend caps and a per-agent rate limit that refuse work before the model runs.
- **Architecture diagrams** in [`docs/architecture/`](docs/architecture/): an illustrated
  overview, the container view, one message end to end, and the decision flow.

### Changed

- **Renamed to सेतु · Setu.** Names and visuals only — routes, data, case ids (`SC-`) and
  behaviour are unchanged.
- **A ticket is raised in milliseconds.** The request saves the message and returns (≈ 6 ms);
  the model works on a pool of four background workers, one per case, and a message sent during
  a reply is merged into the next turn rather than lost.
- **One pipeline, two callers.** The customer turn lives in [`app/pipeline.py`](app/pipeline.py);
  the portal and the console both call it, so there is no second analyse-or-reply path.
- **`allow actions` withholds the write tools** instead of declining to run them, and customers'
  turns never carry them at all.
- **`lead` is retired** and folded into `admin`; accounts still on it are moved on boot. A wrong
  turn now sends a user to their own workspace instead of a 403 page.
- **Every model call is bounded:** 20 s per request and 45 s for the whole call, with no attempt
  started past the ceiling. A slow turn skips the order look-up after 25 s and the bespoke
  handoff note after 35 s.
- The default model is `gemini-3.5-flash-lite`, falling back to `gemini-3.5-flash`.

### Fixed

- **High meant "a complaint about money", not real trouble.** 58 % of live messages were being
  escalated; the thresholds were recalibrated on the live model so a routine "recharge failed,
  money taken" stays with the assistant.
- **A dropped Gemini connection surfaced as a raw error.** Transport failures are now retried
  like a busy server, and when the model is unreachable the case goes to a person with a
  "we have your message" note — nobody sees a stack trace.
- **The 45 s ceiling could be overshot** (59.9 s measured); it is now enforced before each attempt.
- **Hindi fell back to English** because the server overwrote the Hindi catalogue.
- **Opening a case showed an empty scorecard** when a suggestion already existed.
- **An account could not sign back in** with the email it registered with.
- **Only agents are recorded as a case's owner**, never a customer.
- **Ticking `allow actions` let the model execute writes** with no human step.
- **The function-calling loop could never chain two look-ups** — results went back with a role
  the API rejects. It now reads a failed order, then looks up that order's refund.
- **A malformed request was retried against every model**; `400 INVALID_ARGUMENT` now fails fast.
- **The console header scrolled the page sideways on a phone.**

### Security

- The customer's view of a case goes through an allow-list serializer, so analysis, severity,
  scores, coaching, look-ups and cost can never reach a customer — including fields added later.
- The test suite can no longer reach the real Gemini API: a guard replaces the client, and live
  tests must opt in.
- `/api/health` tells an anonymous caller only that the process is up.

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

[Unreleased]: https://github.com/AmanKumar-23/setu/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/AmanKumar-23/setu/compare/v0.1.0...v1.0.0
[0.1.0]: https://github.com/AmanKumar-23/setu/releases/tag/v0.1.0
