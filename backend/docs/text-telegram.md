# Text approvals by Telegram

An approver can approve or reject a request by tapping a button in a Telegram message.
A tap is only a **vote**: it goes through `approval_service.record_vote` with the same
checks and audit trail as a voice decision, and the policy engine decides the request.
The Claude Code guard and the MCP server are unchanged: they read `status` and
`evaluation.authorized` as before.

Everything is **off by default** and additive:

- `TEXT_PROVIDER=disabled` (default): nothing text-related runs.
- A policy's `approval_channel` is `voice` by default. Voice policies behave exactly as
  before; `tests/integration/test_voice_channel_regression.py` pins their jobs and audit
  events.

**Status:** Telegram approve and reject were verified live on 2026-11-04. The security
checks and `text_then_voice` are mock-verified.

## Text first, then voice (`text_then_voice`)

- **The delayed call** is an ordinary call whose initiation job runs at
  `now + voice_fallback_after_seconds`. When its turn comes it cancels itself if the
  request is no longer pending, or if the approver's own seat has already voted (outcome
  `seat_decided`). The seat check applies only to `text_then_voice` requests; voice-only
  requests behave exactly as before.
- **If the message cannot be sent** (any send error, or a revoked link), the delayed call
  is moved to now (`voice.fallback_expedited`). A retried message may still arrive.
- **Both channels can answer, and the first valid vote wins.**
  - A tap after the call has started is recorded normally. The call's later answer is then
    refused and audited (`voice.decision_rejected`, `vote_conflict`), or recorded once if
    it is the same decision (`vote_already_recorded`).
  - A spoken decision first makes a later tap "Not accepted." (`request_closed`, or
    `vote_conflict` while other seats are still open).
- **Escalation and re-approval** of a `text_then_voice` request use calls.
- **The guard must outlast the fallback and a whole call.** With the default 300 s guard
  wait, fallbacks up to about 60 s are covered. The doctor's `Voice fallback` check prints
  the guard wait needed (fallback + about 240 s).

## Behaviour

| Policy `approval_channel` | When a request is created (with `auto_dial` on) |
|---|---|
| `voice` (default) | Calls are scheduled exactly as before. |
| `text` | One Telegram prompt per primary approver with a verified link. **No call is ever placed**, even with `auto_dial` on. |
| `text_then_voice` | A Telegram prompt now and, for each approver who got one, a call delayed by `voice_fallback_after_seconds` (5-600, default 60). Approvers without a verified link, or with text off, are called at once. |

- **The channel is frozen** in the request's snapshot (`approval_channel`, written only
  for non-voice policies).
- **Escalation and re-approval** of a `text` request go by Telegram.
- **No path dials a `text` request:**
  - `voice_service.create_call` refuses it (`text_channel`).
  - `POST /requests/{id}/voice-calls` returns 400 `text_channel`.
- **An approver with no verified link** gets no prompt. This is audited
  (`text.prompt_failed`, reason `no_verified_link`), and the request stays pending
  until its deadline, so the guard fails closed.

## Pairing (one-time links)

```
python -m app.cli pair-telegram --approver-id <id>
#   or POST /api/v1/approvers/{id}/channel-links   (approvers:write)
```

1. **Issuing a link.** Each run prints `https://t.me/<bot>?start=<code>`:
   - The code is valid for **10 minutes**, and only its SHA-256 is stored.
   - Issuing a new link invalidates the previous unused one.
2. **Linking.** The approver presses **Start** in a private chat with the bot. Telegram
   delivers `/start <code>` together with the sender's user ID over our authenticated
   long poll. The bot replies "Linked to <name>".
3. **Rules:**
   - One verified link per approver per channel. Re-pairing replaces the old link
     (`channel.link_revoked` with reason `replaced`).
   - One approver per Telegram account, across all organizations.
   - A used, revoked or expired code is refused. A used code tried by another account
     is audited (`channel.link_rejected`).
4. **Managing links:**
   - `GET /approvers/{id}/channel-links` lists the links (`approvers:read`).
   - `DELETE /approvers/{id}/channel-links` revokes them. Open prompts then stop
     accepting taps.
   - Responses never contain Telegram IDs or codes.

## Prompts and taps

The text worker is a **separate process** (`python -m app.workers.text_worker`).
`start-demo.ps1` starts it only when `TEXT_PROVIDER` is not `disabled`. Run exactly one
per bot, because Telegram allows a single `getUpdates` consumer.

**The message.**
- It shows the same sanitised context the voice agent reads out (`voice/context.py`),
  the request reference, and **Approve** / **Reject** buttons.
- It is sent as plain text, never parsed as formatting.
- Sending follows the voice pattern: no transaction is held across the HTTP call.
- Retryable errors are retried up to `TEXT_SEND_MAX_ATTEMPTS`. A final failure is
  audited as `text.prompt_failed`.

**Button tokens.**
- Format: `al1:<action>:<nonce>:<HMAC-SHA256 under TEXT_CALLBACK_SECRET>`, at most
  64 bytes.
- The HMAC binds the action to the nonce, so swapping Reject for Approve breaks it.
- Only `SHA-256(nonce)` is stored.
- A token is valid only while the prompt is in the status its button belongs to.
- Buttons expire at the earlier of `TEXT_PROMPT_TTL_SECONDS` and the request deadline.

**Approve needs two taps:** Approve, then **Confirm approve** (Cancel goes back with
fresh tokens). Reject is one tap.

**Checks for every tap, in order.** The tap is refused, audited and answered
"Not accepted." at the first that fails:

| # | Check | Refusal reason |
|---|---|---|
| 1 | HMAC signature | `bad_signature` |
| 2 | Prompt exists and is unused (right status, right stage) | `unknown_token` (logged only) / `token_used` |
| 3 | Not expired | `token_expired` (prompt closed, buttons removed) |
| 4 | Sender's user ID, chat and message match the verified link | `link_revoked`, `sender_mismatch`, `wrong_chat` |
| 5 | Request still pending | `request_closed` (prompt closed) |

If every check passes, one transaction does all of the following, locking the request
first and the prompt second:

1. Consume the prompt.
2. Call `approval_service.record_vote(source="telegram", source_details={"text_prompt_id": ...})`.
3. Write `text.decision_accepted`.

If the approval service refuses the vote (`vote_conflict`, `seat_already_decided`,
`condition_open`, ...), the prompt stays consumed and `text.decision_rejected` records
the code.

**Free-text messages are never counted.** The bot replies: "Please use the buttons ...".

**The worker also removes the buttons** from prompts whose request was decided some
other way (another approver, the API, the deadline).

## Audit events

| Event | Subject | Data |
|---|---|---|
| `text.prompt_sent` | request | `text_prompt_id`, `approver_id`, `reason`, `attempt`, `provider`, `expires_at` |
| `text.prompt_failed` | request | `approver_id`, `reason` (`no_verified_link`, `text_unavailable`, a send error code, `link_revoked`), `stage` or `attempts` |
| `text.decision_accepted` | request | `text_prompt_id`, `vote`, `vote_id`, `request_status` |
| `text.decision_rejected` | request | `text_prompt_id`, `reason`, `button` |
| `channel.link_created` / `link_revoked` / `link_rejected` | approver (`approver_id`) | `link_id`, `channel`, `reason`, ... |

`vote.recorded` carries `source: "telegram"`. Audit data never contains Telegram IDs,
tokens or codes.

## Settings (`backend/.env`)

| Setting | Notes |
|---|---|
| `TEXT_PROVIDER` | `disabled` (default) / `telegram` / `mock`. `mock` is refused in production. |
| `TELEGRAM_BOT_TOKEN` | From @BotFather. **Secret.** It is part of every API URL, so `httpx`/`httpcore` logging is pinned to WARNING, and the adapter never raises or logs a URL, exception text or response body. |
| `TELEGRAM_BOT_USERNAME` | Used to build `t.me` links. |
| `TEXT_CALLBACK_SECRET` | At least 32 characters: `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TEXT_PROMPT_TTL_SECONDS` | Default 900. |
| `TEXT_POLL_TIMEOUT_SECONDS` | Default 3. The long-poll wait, which is also the worst-case send delay. |

Switching a policy: `python -m app.cli set-channel --policy-id <id> --channel text`
(back with `--channel voice`). It is audited and bumps the version. The equivalent API
call is `PATCH /policies/{id}` with `{"approval_channel": "text"}`.

## Mock mode (no network)

Set `TEXT_PROVIDER=mock`, `TEXT_CALLBACK_SECRET` and `TEXT_MOCK_STATE_DIR=./.mock_text`,
then run the text worker and use:

```
python -m app.cli pair-telegram --approver-id <id>
python -m app.cli text-simulate --start-url "<printed link>" --from-user 7000001
python -m app.cli text-simulate --prompt-id <text prompt id> --action approve   # then --action confirm
python -m app.cli text-simulate --outbox
```

## Migration 0007

0007 is additive. Its downgrade **refuses** while any of the following exist:

- a text prompt;
- an approver-subject audit event;
- a policy not on `voice`;
- a snapshot carrying `approval_channel`.

Without that guard, dropping the column would silently turn a text-only policy back
into phone calls. To roll back:

1. Run `set-channel --channel voice` on every policy.
2. If the guard still refuses (text prompts or link audit events exist), restore the
   pre-0007 database dump.
