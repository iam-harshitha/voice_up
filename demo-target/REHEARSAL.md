# Rehearsal matrix

Rehearse every row once before presenting, on the setup you'll present with.

**Before you start:**
1. Run `..\scripts\demo-doctor.ps1` in the Claude Code window. The verdict must be `READY`.
2. For a faster rehearsal, set `$env:APPROVAL_LINE_GUARD_TIMEOUT_SECONDS = "60"` before
   starting `claude`.
3. Delete `output\*.json` afterwards.

**What the hook does:**
- **Exit 0:** the command runs. The exact deploy command is pre-allowed, so it runs with no
  second prompt.
- **Exit 2:** the command is blocked, and Claude sees the reason, the request ID and the
  STOP instruction.

In every blocked row, Claude should report the reason and the request ID and then **stop**.
It must not retry, and must not try another command; `CLAUDE.md` and the block message both
tell it so.

| # | Scenario | What you do | Approval request (dashboard) | Call (dashboard) | Hook / Claude Code |
|---|---|---|---|---|---|
| 1 | **Approve** | Ask Claude to run `./deploy.sh production`. Answer: "Yes, this is <name>", then "I approve", then "Yes" | `approved`, authorized | `completed`, decision `accepted` / approve | **Exit 0.** The command runs with no second prompt, and `output/deploy-production-*.json` appears |
| 2 | **Reject** | Same, but say "I reject" | `rejected` | `completed`, `accepted` / reject | **Exit 2:** "Rejected by an approver. The command was not run." Claude stops |
| 3 | **"Not sure"** | Answer "I'm not sure, I need to check first" | stays `pending` (no vote) | `completed`, `no_decision` / `uncertain` (or `needs_clarification`) | Waits until the guard timeout, then **exit 2:** "No decision before the timeout (N s)". Claude stops |
| 4 | **No answer** | Let the phone ring out (or decline) | stays `pending` | `failed` / `no-answer` (or `busy`) | **Exit 2** at the timeout: "No decision before the timeout". No automatic redial |
| 5 | **Retry after a block** | After row 2, type "try again" | the row-2 request stays `rejected` | none new, unless you insist | Claude should refuse and ask you, citing the block. If you explicitly tell it to run the command again, that's a **new** tool call: a new request and a **new phone call**. Expected, but don't do it on stage by accident |
| 6 | **Wrong person answers** | Someone else answers and says "No, this isn't <name>" | stays `pending` | `completed`, `no_decision` / `identity_not_confirmed`. The agent hangs up without describing the request | **Exit 2** at the timeout: "No decision before the timeout" |
| 7a | **Webhook can't reach you** (tunnel down, or Bolna has an old URL) | Stop the tunnel, or leave a stale URL in Bolna, then approve on the phone | `approved` only after the worker polls Bolna (after `VOICE_RECONCILE_AFTER_SECONDS`, e.g. 120 s); otherwise `pending` | after polling: `completed` | Within the guard timeout: exit 0. Otherwise **exit 2** "No decision before the timeout" |
| 7b | **Bolna refuses the call** (trial number not verified, wallet empty) | Ask for the deploy | `pending` | `failed` / `initiation_rejected`. The worker log shows `provider_message` | **Exit 2** at the timeout. Switch to the mock backup |
| 7c | **The API is down** | Stop the API, then ask for the deploy | (none) | (none) | **Exit 2 at once:** "the approval request could not be created (backend_unreachable)" |
| 8 | **Harmless command** | Ask Claude to list the files | (none) | (none) | **Exit 0** at once: no network call, no request |
| 9 | **`git push`** (optional) | Ask Claude to push; approve by phone | `approved` | `completed` | **Exit 0**, then Claude Code's **normal** confirmation prompt: only the deploy is pre-allowed |

## Mock backup (no network, no phone)

Use this when Bolna, the tunnel or the network fails. Say clearly that the calls are
simulated; the dashboard labels them **SIMULATED**.

1. Stop the stack, from the repo root:
   ```powershell
   .\scripts\stop-demo.ps1
   ```
2. In `backend\.env`, switch to the mock provider (no other change is needed):
   ```
   VOICE_PROVIDER=mock
   VOICE_MOCK_STATE_DIR=./.mock_voice
   ```
   `VOICE_WEBHOOK_TOKEN` stays as it is.
3. Start again. With the mock provider no tunnel is started, and the banner says
   SIMULATED VOICE:
   ```powershell
   .\scripts\start-demo.ps1
   ```
4. In the Claude Code window, ask for the deploy as usual. The guard and auto-dial behave
   the same, and the call appears in the dashboard (request page > Voice calls).
5. Answer the simulated call from a second terminal in `backend\`, using the call ID from
   the dashboard:
   ```powershell
   ..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --decision approve
   ..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --decision reject
   ..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --decision uncertain
   ..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --identity no
   ..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --status no-answer
   ```
   These give the same results as rows 1, 2, 3, 6 and 4 above.
6. Afterwards, set `VOICE_PROVIDER=bolna` again and remove `VOICE_MOCK_STATE_DIR`, then
   restart. The tunnel URL will be **new**, so paste it into Bolna again and re-run the
   doctor.
