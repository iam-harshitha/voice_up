# Voice approvals with Bolna

This guide covers how Approval Line places approval phone calls through
[Bolna](https://www.bolna.ai) and turns an approver's spoken decision into a vote. It
also covers what to configure when credentials arrive, and what is verified versus
assumed.

> **Status (2026-10-28):** one real end-to-end call has succeeded: an explicit spoken
> approval became a voice vote and the request became `approved` with
> `evaluation.authorized: true` (see [Live verification](#live-verification)). Every
> other path, including rejection, uncertainty, conditional approval and no-answer, is
> verified only with the mock provider and a mocked HTTP transport. This is a verified
> demo setup on a Bolna trial account behind a quick tunnel, **not** a production
> deployment.

## How it works

```
agent ── POST /requests ──▶ request (pending, policy snapshot)
         └─ policy.auto_dial = true ──▶ voice_calls + voice_jobs, same transaction
operator ── POST /requests/{id}/voice-calls ──▶ voice_calls + voice_jobs (one per approver)
worker ── claims job (SKIP LOCKED, leased) ──▶ Bolna POST /call ──▶ phone rings
Bolna ── webhook on each status change ──▶ /api/v1/voice/webhooks/bolna ──▶ inbox row
worker ── terminal status? GET /executions/{id} (our API key) ──▶ interpret decision
       ──▶ approval_service.record_vote ──▶ deterministic policy ──▶ approved/rejected
```

- **The webhook is only a signal.** Bolna does not sign webhooks. For a final status, the
  worker re-fetches the execution from Bolna's API with our key and reads the decision
  only from that response. A forged or replayed webhook body cannot choose a decision.
- **Votes go through the existing approval service.** The webhook path never sets a
  request's status. The vote, the call outcome, the inbox event and their audit events
  commit in one transaction (`record_vote` runs in a savepoint).
- **Anything other than an explicit, confident approve/reject is not a vote.** That
  includes unanswered, busy, voicemail, uncertain, needs-clarification, malformed or
  low-confidence answers, an unconfirmed identity, a mismatched agent or number, and a
  missing or mismatched correlation ID. The call records a safe `outcome`, and the
  request stays pending.

### Modules

| Module | Responsibility |
|---|---|
| `app/voice/base.py` | Internal types, call lifecycle, provider interface, error kinds |
| `app/voice/bolna.py` | Bolna HTTP adapter and payload normalisation (only place with Bolna field names) |
| `app/voice/mock.py` | Deterministic Bolna-shaped mock (tests and local development only) |
| `app/voice/context.py` | Per-call variables explaining the request (filtered, bounded) |
| `app/voice/decision.py` | Pure interpretation: execution → vote candidate or non-decision reason |
| `app/services/voice_service.py` | Scheduling calls from the snapshot; listing calls |
| `app/services/voice_job_service.py` | Postgres job queue (claim/lease/backoff) and initiate/process/reconcile handlers |
| `app/services/voice_webhook_service.py` | Webhook ingestion (dedup inbox) and transactional application |
| `app/api/v1/endpoints/voice_calls.py`, `voice_webhooks.py` | HTTP endpoints |
| `app/workers/voice_worker.py` | Worker process |

### Call states

`queued → initiating → initiated → ringing → in_progress → processing → completed`.
The terminal states are `completed`, `failed`, `timed_out`, `canceled` and `unknown`.
Transitions only move forward, and terminal states are final, so a late or out-of-order
event cannot regress a call. `unknown` means Bolna *may* have placed the call (for
example, the request timed out after it was sent). Such a call is **never re-dialled
automatically**.

Separately, `decision_status` is `awaiting`, `accepted` (vote recorded), `no_decision`
or `refused` (the approval service refused the vote, e.g. the request was already
decided or the approver had already voted differently).

## Verified Bolna contract

Verified against the official docs on **2026-10-24**, re-checked on **2026-10-28**:

| Topic | Contract | Source |
|---|---|---|
| Auth | `Authorization: Bearer <api key>` | [make call](https://www.bolna.ai/docs/api-reference/calls/make), [API quickstart](https://www.bolna.ai/docs/quickstarts/api) |
| Place call | `POST https://api.bolna.ai/call` with `agent_id`, `recipient_phone_number` (E.164), optional `from_phone_number`, `user_data` (per-call variables) | [make call](https://www.bolna.ai/docs/api-reference/calls/make) |
| Response | `200 {"message", "status": "queued", "execution_id"}`; `400 {"error", "message"}` | same |
| Read call | `GET /executions/{execution_id}` returns the execution object | [get execution](https://www.bolna.ai/docs/api-reference/executions/get_execution) |
| Webhook | `POST` of the same execution object, sent on every status change, from IPs `13.203.39.153`, `13.126.9.249`, `13.202.133.53`. Custom headers can be added. No signature and no event ID; key idempotency on `id`. | [webhooks](https://www.bolna.ai/docs/polling-call-status-webhooks), [payload](https://www.bolna.ai/docs/api-reference/executions/payload), [extractions tab](https://www.bolna.ai/docs/agent-setup/analytics-tab) |
| Statuses | `scheduled, queued, rescheduled, initiated, ringing, in-progress, call-disconnected, completed, balance-low, busy, no-answer, canceled, failed, stopped, error`. `completed` is final, after extraction finishes. | [statuses](https://www.bolna.ai/docs/list-phone-call-status) |
| Decisions | `extracted_data[<Category>][<Extraction>] = {objective, subjective, confidence (0–1), confidence_label, reasoning_objective, reasoning_subjective, validation}`; `objective` holds the selected pre-defined answer, `subjective` the free text. Keys are the display names as typed in the dashboard (confirmed live). | [extraction output](https://www.bolna.ai/docs/prompting/extraction-output), [payload](https://www.bolna.ai/docs/api-reference/executions/payload) |
| Prompt variables | `user_data` keys are referenced as `{{name}}` in the prompt and welcome message | [make call](https://www.bolna.ai/docs/api-reference/calls/make), [API quickstart](https://www.bolna.ai/docs/quickstarts/api) |
| Variables echo | Per-call variables come back in `context_details.recipient_data` | [extractions migration](https://www.bolna.ai/docs/migrating-to-extractions/overview), [payload](https://www.bolna.ai/docs/api-reference/executions/payload) |
| Rate limit | HTTP `429`; no published request rate | [limits](https://www.bolna.ai/docs/api-reference/limits) |

### Not verified or not available (designed around)

- **No idempotency key for `POST /call`.** Failures after the request may have reached
  Bolna (read timeout, 500/502/504, malformed 200) are classified as ambiguous and not
  retried. Only connection failures, 429 and 503 are retried. Treating 503 as "not
  accepted" is our assumption.
- **No webhook signature, timestamp or event ID.** We use a shared-secret custom header
  plus re-fetching terminal states. Deliveries are deduplicated on
  `sha256(execution id | status | updated_at)`.
- **Whether Bolna retries failed webhook deliveries is unclear.** The limits page says it
  retries; the workflow-webhook page says it does not. We answer 2xx only after the
  event is committed, return 503 on storage failure, and poll Bolna for calls without a
  final update (reconciliation), so a lost webhook is recovered either way.
- **PII masking.** If it's enabled on the account, `extracted_data` string values become
  `null`, so no decision can ever be read. Keep masking off for the approval agent, or
  expect every call to end as `identity_not_confirmed`. (It was off on the account used
  for the live test.)
- **Correlation.** An execution is accepted only if its `id` is the `execution_id` that
  Bolna returned to *our* `POST /call`, its `agent_id` is ours, and the echoed
  `context_details.recipient_data.approval_line_call_id` is present and equals the
  internal call ID. A missing or different value is `correlation_mismatch` and no vote.
  `telephony_data.to_number` (or `context_details.recipient_phone_number`) must equal the
  dialled number **when present**; it is not required, because PII masking nulls phone
  numbers. Bolna returned both values in the live test.

## Configuring Bolna

Settings in `backend/.env` (placeholders only; never commit real values):

```
VOICE_PROVIDER=bolna                      # mock for local development and tests
BOLNA_API_KEY=<from the Bolna dashboard>
BOLNA_AGENT_ID=<agent UUID>               # a UUID, not the agent's display slug
VOICE_WEBHOOK_TOKEN=<python -c "import secrets;print(secrets.token_urlsafe(32))">
BOLNA_EXTRACTION_CATEGORY=Decision
BOLNA_DECISION_EXTRACTION=Approval
# BOLNA_IDENTITY_EXTRACTION=Identity Confirmed   (default)
# BOLNA_CONDITION_EXTRACTION=Condition           (default)
# BOLNA_FROM_PHONE_NUMBER=+91XXXXXXXXXX          (optional; Bolna's default otherwise)
# VOICE_RECONCILE_AFTER_SECONDS=120              (default 600; lower for demos)
```

The code defaults for the category and decision names are `Approval` and `Decision`
(the reverse of the agent used here), so set both explicitly to whatever your agent's
Extractions tab shows. A mismatch fails closed: every call ends `identity_not_confirmed`.

1. **API key:** create one in the Bolna dashboard and set `BOLNA_API_KEY`. Never put it
   in code.
2. **Agent:** create an outbound voice agent and set `BOLNA_AGENT_ID`. Its welcome
   message and prompt use the per-call variables below, written as `{{name}}` (the
   documented syntax; single braces are read aloud literally). Use no other variables;
   Approval Line sends only these:

   | Variable | Meaning |
   |---|---|
   | `approver_name` | Who must answer |
   | `requested_action`, `request_summary` | What is being approved |
   | `request_details`, `risk_details` | Selected context (secrets and contact data removed) |
   | `request_reference` | Short reference to read out |
   | `required_approvals` | 1 or 2 |
   | `allowed_decisions` | "approve or reject" |
   | `approval_line_call_id` | Correlation ID. Never read it aloud. |

   Welcome message used in the live test: "Hello, this is the automated approval
   assistant from Approval Line. Am I speaking with {{approver_name}}?"

   Prompt outline (the live test used a longer version of this):
   > You are an automated assistant from Approval Line calling **{{approver_name}}** for
   > an approval decision. First confirm you are speaking with {{approver_name}}; if
   > not, end politely without describing the request. Explain: "An automated agent is
   > requesting approval to {{requested_action}}. Summary: {{request_summary}}. Details:
   > {{request_details}}. Risk: {{risk_details}}. The reference is
   > {{request_reference}}. This request needs {{required_approvals}} approval(s)."
   > Say clearly that **nothing has been done yet**. Ask whether they approve or reject
   > ({{allowed_decisions}}). Answer questions only from these facts; never invent
   > details. Never encourage approval. If they are unsure, or want conditions or more
   > information, do not record approval. Before ending, repeat their decision back
   > with the reference and ask them to confirm it. Never read out
   > approval_line_call_id.
3. **Extractions:** in the agent's Extractions tab, create one category (the live agent
   uses **`Decision`**) with:
   - **`Approval`** (pre-defined answers), with answers exactly `approve`, `reject`,
     `uncertain`, `needs_clarification` and `conditional_approve`. Prompt: "The
     approver's final, confirmed decision. Use approve or reject only if they explicitly
     and unconditionally said so and confirmed it. Use conditional_approve if they
     approve only once something else happens. Otherwise use uncertain or
     needs_clarification."
   - **`Identity Confirmed`** (pre-defined answers), with answers `yes` and `no`.
     Prompt: "Did the person confirm they are {approver_name}?"
   - **`Condition`** (free text). Prompt: "If the approver approved only on a
     condition, state the condition in one sentence; otherwise leave empty."
     Approval Line stores this text; it never interprets it, and a conditional answer
     never becomes a vote (see `policies.md`). A `conditional_approve` without condition
     text is recorded as `condition_missing` and treated as no decision.

   Set `BOLNA_EXTRACTION_CATEGORY`, `BOLNA_DECISION_EXTRACTION`,
   `BOLNA_IDENTITY_EXTRACTION` and `BOLNA_CONDITION_EXTRACTION` to the names exactly as
   shown in the dashboard (they are case- and space-sensitive). Free-text answers come
   back in `subjective` (confirmed live).
4. **Webhook:** in the Extractions tab's webhook card, set the URL to
   `https://<public-host>/api/v1/voice/webhooks/bolna`. In **Headers (JSON)** add
   `{"X-Approval-Line-Webhook-Token": "<VOICE_WEBHOOK_TOKEN>"}`, then **Save agent**.
   Leave "Trigger on statuses" at all statuses. Allowlist Bolna's three IPs at your
   proxy or firewall (not enforced by the application).
5. **Telephony:** outbound calls need a telephony provider for the destination country
   (the live agent used Vobiz for India; Bolna lists Twilio for most other countries).
   **Trial accounts can only call verified numbers**: verify the approver's number in
   the Bolna dashboard first, or `POST /call` returns 400 and the call ends
   `failed` / `initiation_rejected`. The worker logs Bolna's sanitized reason as
   `provider_message` on `voice.call_initiation_rejected`.
6. **Server:** set `VOICE_PROVIDER=bolna`, restart the API (from `backend/`, so it reads
   `backend/.env`), and start the worker. Startup fails if the key, agent ID or webhook
   token is missing. It never falls back to the mock. By default calls are placed only
   for an explicit `POST /requests/{id}/voice-calls` (or the dashboard's **Schedule
   calls** button): creating a request never dials anyone, unless its policy has
   `auto_dial` switched on (next section).

### Auto-dial (opt-in per policy)

A policy with `"auto_dial": true` (default `false`; set on `POST /policies` or
`PATCH /policies/{id}`, or with the dashboard's **Auto-dial approvers** checkbox) has its
approvers' calls scheduled as soon as a request is created under it. Changing it bumps
the policy `version` and is recorded in `policy.updated` (`changed_fields`).

- **Same code path as the button.** `voice_service.auto_dial` calls the same scheduling
  function as `POST /requests/{id}/voice-calls`, so the duplicate-call guard, the
  generation limit and the skipped-approver rules are identical. The call's
  `voice.call_scheduled` event has `reason: "auto_dial"`, and one
  `voice.auto_dial_scheduled` event records the `policy_id`, `policy_version`, the call
  IDs and any skipped approvers.
- **Server-side.** The agent's key needs only `requests:create`; it never needs
  `voice:calls:create`.
- **Exactly once.** Scheduling runs inside the request's creation transaction
  (`approval_service.create_request`'s `on_created` hook), so the calls exist if and only
  if the request does. An idempotent replay (same `Idempotency-Key`) returns the original
  request without scheduling anything; a concurrent retry with the same key loses on the
  key's unique index and rolls back entirely, calls included. A later manual "Schedule
  calls" finds the calls and creates none. The worker still dials only queued jobs.
- **Failures never block the request and never dial.** If scheduling fails (voice
  disabled, no approver with a phone number, a database error), that attempt is rolled
  back to a savepoint, the request is still created `pending`, and
  `voice.auto_dial_failed` records the `policy_id`, `policy_version` and `reason`
  (`voice_unavailable`, `no_callable_approvers`, `scheduling_error`, …). An operator can
  then schedule calls manually. Nothing is retried automatically.
- **Status:** covered by `tests/integration/test_auto_dial.py` with the mock provider, and
  live-verified with Bolna on 2026-10-29 (see Live verification below).

### Bolna's "Send test" button returns 400, by design

The dashboard's **Send test** posts a fixed sample, not a real execution: its `id` is a
21-character slug rather than a UUID, its `agent_id` is a short slug, and its
`extracted_data` uses the retired flat layout. The endpoint checks the token first, so:

| Send test result | Meaning |
|---|---|
| `400 invalid_payload` | Token accepted; the sample was refused at the UUID check. **This is the expected result.** |
| `401 webhook_unauthorized` | The header is missing or holds a different token. |
| `404` | Voice is disabled (`VOICE_PROVIDER=disabled`) or no token is configured. |
| 502 / timeout | The API or the tunnel is down. |

Do not relax the UUID check to make Send test return 200. The execution ID is put into
the `GET /executions/{id}` URL, and accepting non-UUID IDs would let a token holder steer
that request. Real executions always carry UUIDs.

### Live verification

First live test, 2026-10-28, one approver, Bolna trial account, Vobiz (India), API
behind a Cloudflare quick tunnel:

- [x] `POST /call` accepted (200, `queued`) after the number was verified in Bolna. The
      two earlier attempts were refused (400, trial account, unverified number), ended
      `failed` / `initiation_rejected`, and recorded no vote.
- [x] The phone rang; five webhooks arrived, each passing the token check. The call moved
      through `queued → in-progress → call-disconnected → completed`. No decision was taken at
      `call-disconnected`.
- [x] On `completed`, the worker re-fetched the execution and found
      `extracted_data["Decision"]` with `Approval` = `approve` (confidence 0.95),
      `Identity Confirmed` = `yes` (0.95) and `Condition` as free text (0.9). Confidence
      was numeric with a `confidence_label` of `High`.
- [x] `context_details.recipient_data` echoed every variable, including
      `approval_line_call_id`, and `telephony_data.to_number` matched the stored number
      exactly.
- [x] Audit trail: `voice.call_initiated → vote.recorded → request.approved →
      voice.decision_accepted`. The request became `approved`, `evaluation.authorized: true`.
- [x] Send test: 400 with the correct token, 401 without (see above).

Second live session, 2026-10-29, same setup, one approver, every call placed by
auto-dial:

- [x] Approval through the Claude Code guard: `approved`, `authorized: true`
      (`quorum_met`), about 2 minutes from request to decision.
- [x] Spoken rejection through the guard: `rejected` (`veto`); the guard exited 2 with
      the STOP instruction.
- [x] "Hmm, I'm not sure": extracted as `uncertain` (confidence 1.0), outcome
      `uncertain`, no vote; the request stayed `pending` / `awaiting_decisions`.
- [x] Identity not clearly confirmed: outcome `identity_not_confirmed`, no vote.
- [x] Declined call: provider status `busy`, call `failed`, no vote, no automatic re-dial;
      the request stayed `pending`.
- [x] Unverified number after a change in Bolna: initiation refused (400), call
      `failed` / `initiation_rejected`, no vote, no retry.

Not yet tested live (covered by mock tests only):

- [ ] Needs clarification.
- [ ] Conditional approval: condition recorded, then resolved, then re-approval call.
- [ ] Unanswered ring-out (`no-answer`) and voicemail.
- [ ] A lost webhook recovered by reconciliation.
- [ ] Two-approver and quorum policies by phone.
- [ ] Any deployment other than the local API behind a quick tunnel.

## Local development without credentials

```powershell
# backend/.env (development only)
VOICE_PROVIDER=mock
VOICE_MOCK_STATE_DIR=./.mock_voice
VOICE_WEBHOOK_TOKEN=<python -c "import secrets;print(secrets.token_urlsafe(32))">

uvicorn app.main:create_app --factory --reload      # terminal 1
python -m app.workers.voice_worker                   # terminal 2 (or --once)
```

With a voice key (`voice:calls:create,voice:calls:read`), schedule calls and let the
worker "dial". Then finish each simulated call and deliver it through the **real,
token-authenticated webhook**:

```powershell
python -m app.cli voice-simulate --call-id <voice call id> --decision approve
python -m app.cli voice-simulate --call-id <id> --decision uncertain     # no vote
python -m app.cli voice-simulate --call-id <id> --status no-answer       # failed, no vote
```

`voice-simulate` refuses to run unless `VOICE_PROVIDER=mock`, `VOICE_MOCK_STATE_DIR` is
set and `APP_ENV` is not `production`. The mock itself is refused in production.

## Security review (2026-10-24)

An independent review of the code found and fixed these defects, each with a
PostgreSQL regression test in `tests/integration/test_voice_review_regressions.py`:

| Severity | Defect | Fix |
|---|---|---|
| Medium | `run_initiate` phase 3 overwrote a call another worker or a trusted provider observation had already resolved. A lost lease left a call stuck `queued`; a fast `busy` adopted as `failed` was overwritten with `unknown`. | Phase 3 changes the call only while it is still `initiating` and unbound; otherwise the result is `superseded`. |
| Low | `apply_execution` could apply an execution to a call that was bound to a different execution between the unlocked lookup and the row lock. | The binding and adoption are re-validated under the lock; mismatches are `unmatched`. |
| Low | Deeply nested webhook JSON caused an unhandled `RecursionError`: a 500 response plus a very large traceback log (CPU and log DoS for a token holder). | Treated as invalid JSON (400). |
| Low | A forged, token-bearing flood of terminal events caused one Bolna API fetch each, which could exhaust the account rate limit and block real calls. | Fetch only for a known open call, or when a call is awaiting adoption. |
| Low | Approvers who had already voted were called anyway. | Skipped with reason `already_voted`. |
| Low | Nothing stopped a job lease shorter than a provider request. | `VOICE_JOB_LEASE_SECONDS` must be at least 4× `VOICE_HTTP_TIMEOUT_SECONDS`. |

## Operating notes and limitations

- **One intended call per approver.** Re-dialling is an explicit operator action
  (`{"redial_unanswered": true}`), bounded by `VOICE_MAX_CALL_GENERATIONS` (default 3
  attempts per approver per request, the first call included). There's no
  escalation chain and no clarification call-back.
- **Exactly-once phone calls are not possible.** The external call can't be part of a
  database transaction. The design guarantees durable jobs, no automatic re-dial after
  an ambiguous outcome, and idempotent local state. A call left `unknown` is adopted
  automatically if Bolna later reports an execution carrying its correlation ID, the
  same agent and the same number. Otherwise it needs a human: check the Bolna dashboard,
  then re-dial explicitly.
- **Webhook authentication is a static shared secret.** There's no replay protection
  beyond deduplication. The impact is limited because webhooks can't set decisions.
  Rotate the token by updating both Bolna's header and `VOICE_WEBHOOK_TOKEN`.
- **Identity is voice-asserted.** "Am I speaking with X?" plus the dialled number is
  not strong authentication. Anyone answering the approver's phone and claiming to be
  them could approve. Consider a spoken PIN or callback verification before production.
- **Database role.** The runtime role owns the tables, so it can disable or drop the
  audit and call-binding triggers. The triggers stop application bugs, not a
  compromised database credential. Before production, split migration-owner and
  runtime roles, and grant the runtime role only `INSERT`/`SELECT` on `audit_events`.
- **A leaked webhook token** still cannot create votes. It can grow `voice_provider_events`
  (and add `voice.event_ignored` audit rows for finished calls) with distinct forged
  deliveries. Rotate the token, and enforce Bolna's IP allowlist at the edge.
- **Processing order.** Deliveries received within the same clock tick may be processed
  in either order. Correctness does not depend on it: lifecycle moves only forward, and
  decisions are re-fetched.
- **Audit trail.** Events are append-only by trigger, not cryptographically
  tamper-proof. Audit data holds IDs and reason codes only: no transcripts, no phone
  numbers.
- **Data kept.** The inbox stores only execution ID, status and `updated_at`. Raw
  bodies, transcripts and recording URLs are never stored. `voice_calls` keeps the
  dialled number to verify executions; the API never returns it.
