# 3-minute demo script: "A coding agent that has to ask a human first"

**Before the judges arrive** (about 10 minutes):
- **Approval Line:** the API and worker are running, and `/api/v1/health/ready` returns `ok`.
- **Bolna, for a live call:** the tunnel is up, its URL is saved in Bolna's webhook
  settings, and the approver's number is verified.
- **The policy:** **Auto-dial approvers** is on, and its single approver is the phone in
  your hand.
- **Claude Code:** a PowerShell window in `<repo>\demo-target` with the three
  `APPROVAL_LINE_*` variables set. `claude` is started there, and `/hooks` shows the guard.
- **Screen:** the dashboard is open in a browser beside Claude Code, on the request list.
- **Preflight:** run `..\scripts\demo-doctor.ps1` in the Claude Code window; the verdict must
  be `READY` (read-only, no calls).
- **Rehearsal:** work through `REHEARSAL.md` (at least approve and reject end to end). Then
  delete `output/*.json`.

---

**0:00 to 0:20 The problem**

> "Coding agents can now run any command: deploy, push, delete. Today the only thing between
> the agent and production is a yes/no prompt on the developer's own screen. Approval Line
> puts a *policy* and a *human phone call* in that path."

Show the terminal: Claude Code is running in `demo-target`.

**0:20 to 0:50 A harmless command passes instantly**

Type to Claude:
> *List the files in this folder.*

The judges see `ls` run at once.

> "Ordinary commands aren't slowed down. The guard checks a rules file and makes no network
> call."

**0:50 to 2:00 A risky command waits for a phone approval**

Type to Claude:
> *Deploy to production by running ./deploy.sh production.*

The judges see:
- **In Claude Code:** the spinner *"Approval Line: waiting for a human to approve by phone"*;
  the command has **not** run.
- **In the dashboard:** a new pending request, "deploy_to_production", with the exact
  command and directory, and a call that is *queued*, then *ringing*.
- **The phone rings:** answer it on speaker.

On the call:
1. "Yes, this is <name>."
2. Listen to the request being read out: what will run, and where.
3. "I approve." Then "Yes" to the read-back.

> "The vote isn't taken from the webhook. The server re-fetches the call result from Bolna,
> checks the identity and the confidence, and the policy engine decides. Only then does the
> guard let the command through, after checking that the approval is for *this exact
> command*."

The judges see the request turn **approved** in the dashboard, then the command run in
Claude Code with no second prompt (the exact deploy command is pre-allowed, and the guard
always runs before that rule), and
`output/deploy-production-*.json` appear.

**2:00 to 2:40 The reverse: rejection**

Type to Claude:
> *Now push the branch with git push.*

Answer the phone and say "I reject."

The judges see Claude Code report: *"Approval Line guard: Rejected by an approver. The
command was not run. (approval request ...)"*. Claude relays it and does not retry on
its own.

> "Every other outcome blocks too: no answer, an unclear answer, a timeout, the backend
> down, a bad config. The default is deny."

**2:40 to 3:00 Close**

Open the request's **audit timeline** in the dashboard: request created, calls scheduled
automatically by policy, call initiated, spoken decision accepted, request approved.

> "A policy-driven, auditable human checkpoint for any agent, delivered as one hook file and
> a phone call."

---

## If something goes wrong

| What you see | What to do or say |
|---|---|
| The phone doesn't ring within about 20 s | Check the worker terminal and the dashboard call status. If Bolna is the problem (trial number, wallet, network), say "the call failed, and look, the command stayed blocked: that's the fail-closed design". Then switch to the mock backup below. |
| Claude Code shows "Approval Line unreachable" | The API or tunnel is down. Point out that the command was **not** run. Restart the API from `backend/` and retry. |
| "No decision before the timeout" | The call wasn't answered in time, or the answer was unclear (e.g. "I'm not sure"). Say "an unclear answer is never treated as approval", then ask Claude again. |
| "has auto-dial off" | The policy's auto-dial is off. Tick **Auto-dial approvers** in the dashboard and retry. |
| Every command is blocked, even `ls` | The rules file is missing or still has placeholders: check `APPROVAL_LINE_GUARD_RULES` and `guard-rules.json`. |
| The approval came through but the command didn't run | For the deploy, the pre-allow rule only applies in a trusted folder: accept the workspace trust dialog. For other commands Claude Code's normal prompt is waiting: approve it on screen. |
| Claude tries the command again, or a different one, after a block | It shouldn't (`CLAUDE.md` and the block message both say stop). Press Esc and say: "It stopped because a human said no; that's the point." |

**Mock backup (no network, no phone):**
1. Before the demo, run the API and worker with `VOICE_PROVIDER=mock`.
2. When the call appears in the dashboard, answer it from a second terminal in `backend/`:
   ```powershell
   ..\env\Scripts\python.exe -m app.cli voice-simulate --call-id <voice-call-id> --decision approve
   ```
3. Say clearly that the call is simulated; the dashboard labels it **SIMULATED**.
