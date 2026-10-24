# Policies, consensus, conditions, deadlines and escalation

This document defines exactly how Approval Line decides a request. The implementation
is the pure engine in `app/domain/approval_rules.py`, plus the transactional services
in `app/services/approval_service.py` (votes, conditions, expiry) and
`app/services/escalation_service.py`. No other code computes a decision.

**Only `status: "approved"` (equivalently `evaluation.authorized: true`) authorizes the
action, and only the action in that request.** Approval Line never executes anything.

## Policy fields

| Field | Values | Meaning |
|---|---|---|
| `approval_mode` | `unanimous` (default), `quorum` | `unanimous`: every primary approver must approve; `required_approvals` must equal their number. `quorum`: `required_approvals` of the primaries must approve. |
| `required_approvals` | 1 or 2 | Seats that must approve. |
| `approver_ids` | 1–5 distinct, active approvers | **Primary approvers**. Each is a *seat*. |
| `veto_mode` | `any_rejection` (default), `designated`, `none` | Which rejections are vetoes (below). `unanimous` requires `any_rejection`. |
| `veto_approver_ids` | subset of `approver_ids` | Veto holders. Required and allowed only with `designated`. |
| `backup_approver_ids` | 0–3, distinct, active, not primaries | Escalation targets, in order. |
| `decision_deadline_seconds` | 60 s – 30 days, or null | A pending request expires this long after creation. Null means no deadline. |
| `escalation_after_seconds` | 60 s – 7 days, or null | Interval between escalation rounds. Requires backups, and must be shorter than the deadline. |
| `max_escalations` | 1 – number of backups (0 when no escalation) | Total escalation steps per request. Defaults to the number of backups. |

All fields default to Chunk 1 behaviour, so existing policies and clients are unchanged.
Every authorization-relevant change bumps `version` and writes a `policy.updated` audit
event (with `changed_fields` and the acting API key). Policies are validated as a whole
on create and update. These are rejected with `approval_policy_invalid`:
- a quorum larger than the number of primaries;
- duplicate approvers, or the same approver as both primary and backup;
- inactive or cross-organization approvers;
- `designated` without veto holders, or veto holders in another mode;
- escalation without backups, or escalation at or after the deadline;
- `max_escalations` without escalation, or outside its range.

## Decision semantics

- **Affirmative vote:** an explicit, validated `approve` from an approver authorized for
  a seat. Through voice, that means an explicit, confident `approve` from a confirmed
  identity on a verified execution (see `voice-bolna.md`).
- **Seats:** each primary holds one seat. A seat is decided by the **first valid vote**
  for it. `UNIQUE(request_id, seat_approver_id)` on votes enforces this in the database.
- **Veto:**
  - `any_rejection`: any seat's rejection rejects the request immediately (reason
    `veto`).
  - `designated`: only veto holders' seats veto. Any other rejection just removes that
    seat from the possible approvals.
  - `none`: no vetoes.
- **Quorum unreachable:** in every mode, once the seats still able to approve number
  fewer than `required_approvals`, the request is rejected (reason
  `quorum_unreachable`).
- **Abstentions and non-responses** are not votes. Unanswered, failed or ambiguous
  calls, timeouts and uncertain answers leave the seat undecided and can never approve.
- **Precedence** for a pending request: veto > quorum unreachable > deadline passed >
  condition > quorum met > waiting.
- **Final decisions are immutable.** Any later vote returns 409, and a late provider
  event is refused.

## Snapshots

At creation, the request stores everything needed to decide it:
- `policy_snapshot` (schema v2): mode, quorum, veto mode, primaries with veto flags,
  ordered backups, deadline, escalation settings, condition rule, policy ID and version;
- `expires_at`, the absolute deadline;
- a normalized, trigger-protected `request_approvers` table.

Votes, conditions and escalation steps have foreign keys into `request_approvers`, so
the database rejects anyone outside the snapshot. Policy edits and approver
deactivation never change an existing request. (An inactive approver still cannot cast
a new vote.) Requests from before Chunk 3 (snapshot v1) keep their original unanimous,
any-rejection semantics.

## Conditional approvals

A conditional approval ("approve only after the backup is verified") is **never a vote**.
- It is stored in `approval_conditions`, with the approver, seat, policy version,
  source (`voice` or `api`), originating call and the normalized description. The text
  is stored as data and never interpreted.
- While a condition is **open**, the request cannot be approved, even if quorum is met:
  `evaluation.state = "waiting_for_condition"`, `reason = "condition_open"`,
  `next_action = "resolve_condition"`. An approve from that seat is refused
  (`condition_open`); a reject is allowed and supersedes the condition.
- Only a key with the `conditions:resolve` scope can resolve it, with an outcome, a
  note and an optional evidence reference. The resolver's key is recorded.
  - `satisfied` **does not approve**. The seat still blocks
    (`reason = "reapproval_required"`) until **the approver who stated the condition**
    gives a fresh, unconditional approve. If voice is enabled, a re-approval call to
    that approver is scheduled in the same transaction.
    - Another holder of the same seat (the primary, when a backup stated the condition,
      or the reverse) cannot approve meanwhile: `409 reapproval_required`. Otherwise
      the seat would be decided without the re-approval and the request could never
      be approved.
    - Any holder of the seat may still **reject** it. That decides the seat (a veto,
      or a seat lost to the quorum) and the condition no longer blocks anything. The
      condition record itself stays `satisfied`.
  - `not_satisfied` voids the conditional approval, and the seat is undecided again.
- Only one approver at a time may hold conditions on a seat. While one holder's
  condition is open or awaits re-approval, a condition from another holder of that seat
  is refused with `409 seat_condition_pending`. So a re-approval always has exactly one
  approver who can give it.
- Repeating the same resolution is idempotent. A different resolution of a closed
  condition returns 409. A trigger freezes a closed condition.
- When a request becomes final, its open conditions become `lapsed`.

## Deadlines

`expires_at = created_at + decision_deadline_seconds` is snapshotted, and a durable
`expire_request` job is queued for it.
- From `expires_at` on, votes and conditions are refused (`request_expired`), and the
  evaluation reports `expired` even before the job runs.
- The job locks the request. If it's still pending, the request becomes `expired` and
  `request.expired` is written in the same transaction. Otherwise the job does nothing,
  so it is idempotent. A job that runs early reschedules itself to `expires_at`.
- Expiry is terminal and never extended. A vote and the expiry job serialize on the
  request row: whichever commits first wins.
- The deadline is judged when the request row lock is **granted**, not when the
  operation started. A vote, condition, resolution or voice decision that waits for the
  lock until after `expires_at` is refused.

## Escalation

At `created_at + k × escalation_after_seconds` (round k), each **outstanding** seat (no
vote, no blocking condition) gets the next unused backup, until `max_escalations` steps
exist or the backups run out.
- The backup **replaces** that seat's primary. Either may decide it; the first valid
  vote wins and a later one gets `seat_already_decided`. A backup inherits the seat's
  veto power. The quorum is never lowered.
- Each step is an immutable `escalation_steps` row (sequence 1–3, round, seat, backup,
  outcome), with a call scheduled through the normal voice path. Without voice, the step
  is still created (outcome `voice_unavailable`), and the backup can answer through the
  trusted vote endpoint.
- **The deadline takes precedence:** no round runs at or after `expires_at`. Terminal
  requests never escalate.
- A round, its audit events and its job's rescheduling commit in **one transaction**,
  which also holds the job row. A re-run of the same round is a no-op.

## Structured states for agents

| `status` | `evaluation.state` | `reason` | `next_action` | Meaning |
|---|---|---|---|---|
| pending | awaiting_approvals | awaiting_decisions | await_decisions | Keep waiting; do not act. |
| pending | waiting_for_condition | condition_open | resolve_condition | An approver set a condition; a human must resolve it. |
| pending | waiting_for_condition | reapproval_required | await_decisions | Condition satisfied; waiting for the approver in `reapproval_approver_ids` to re-approve. |
| approved | approved | quorum_met | none | **Authorized** for this action only. |
| rejected | rejected | veto / quorum_unreachable | none | Do not act. |
| expired | expired | deadline_passed | none | Do not act; create a new request if still needed. |

`evaluation` also returns:
- `approvals`, `rejections`, `required_approvals`, `veto_triggered`;
- `outstanding_approver_ids` and `condition_approver_ids`, which are seat (primary)
  IDs;
- `reapproval_approver_ids`, the approvers who must re-approve (each one the stater of
  a satisfied condition, possibly a backup), and `reapproval_seat_ids`, their seats in
  the same order;
- `deadline_passed` and `expires_at`.

## Audit events

| Event | Written when |
|---|---|
| `request.created` | The request is created, with the snapshot summary. |
| `vote.recorded` | A vote is recorded: source, seat, quorum progress. |
| `request.approved`, `request.rejected`, `request.expired` | The request becomes final. Carries the evaluation, plus `approve_votes`/`reject_votes`. |
| `condition.created`, `condition.resolved`, `condition.superseded`, `condition.lapsed` | The condition lifecycle. |
| `condition.reapproval_requested` | A satisfied condition triggers re-approval. |
| `escalation.step_created`, `escalation.exhausted` | An escalation step is created, or the chain runs out. |
| `policy.created`, `policy.updated` | A policy changes. Carries `policy_id`, the version, the changed fields and the actor key. |
| `voice.*` | The call lifecycle and voice decisions (Chunk 2). |

- **Atomicity:** each event commits in the same transaction as the change it describes.
- **Reading:** `GET /requests/{id}/audit-events` and `GET /policies/{id}/audit-events`
  require the `audit:read` scope. They page with keyset pagination on `sequence`
  (`?after=&limit≤200`). `sequence` reflects insert order, so a page read while
  transactions are still in flight can miss an event that commits later with a lower
  number; timelines of finished requests are complete.
- **No sensitive data:** event data never contains keys, tokens, phone numbers or
  transcripts. A condition's text is stored on the condition, not in the audit data.

**Not tamper-proof:**
- **The database role can bypass the triggers.** Triggers block `UPDATE`/`DELETE` on
  audit events, snapshot approvers, escalation steps and closed conditions. But the
  runtime role owns these tables, so it could disable or drop the triggers.
- **Hardening:** run migrations as a separate owner role. Grant the runtime role only
  `SELECT, INSERT` on `audit_events`, `request_approvers` and `escalation_steps`, and
  `SELECT, INSERT, UPDATE` on `approval_conditions`. This hasn't been applied to any
  local database.
- **Provider events have weak authentication.** Bolna webhooks are authenticated only
  by a shared token. Decisions are re-fetched from Bolna with our key (see
  `voice-bolna.md`).

## Endpoints and scopes

| Endpoint | Scope |
|---|---|
| `GET /requests/{id}`, which now includes `evaluation` and `expires_at` | `requests:read` |
| `GET /requests/{id}/conditions`, `GET /requests/{id}/escalations` | `requests:read` |
| `POST /requests/{id}/conditions` | `votes:write` (trusted integration, like votes) |
| `POST /requests/{id}/conditions/{cid}/resolve` | `conditions:resolve` |
| `GET /requests/{id}/audit-events`, `GET /policies/{id}/audit-events` | `audit:read` |

The default agent key is still only `requests:create, requests:read`.

## Two-approver walkthrough (mock voice)

With `VOICE_PROVIDER=mock`, `VOICE_MOCK_STATE_DIR` and `VOICE_WEBHOOK_TOKEN` set, and
keys for an agent, an admin, voice (`voice:calls:create,voice:calls:read`), a resolver
(`conditions:resolve`) and an auditor (`audit:read`):

1. Create a policy with two primaries and `decision_deadline_seconds: 3600`, then
   create a request, then `POST /requests/{id}/voice-calls`. Run
   `python -m app.workers.voice_worker --once`.
2. `python -m app.cli voice-simulate --call-id <alice call> --decision approve`, then
   run the worker. The request shows `pending` / `awaiting_approvals`.
3. `python -m app.cli voice-simulate --call-id <bob call> --decision conditional_approve
   --condition "only after the database backup is verified"`, then run the worker. The
   request shows `pending` / `waiting_for_condition` / `resolve_condition`.
   - *Variant:* `--decision reject` instead gives `rejected` / `veto`.
4. Call `POST /requests/{id}/conditions/{cid}/resolve` with `{"outcome": "satisfied",
   "note": ...}`. The request shows `pending` / `reapproval_required`, and a
   generation-2 call to Bob is queued.
5. Run the worker, then `voice-simulate --call-id <bob's new call> --decision approve`,
   then the worker again. The request shows `approved` / `quorum_met` /
   `authorized: true`.
6. `GET /requests/{id}/audit-events` returns the full sequence: created → calls →
   Alice's vote → condition created → resolved → re-approval requested → Bob's vote →
   approved.
