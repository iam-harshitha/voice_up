# Rules for this project (Approval Line guard demo)

Risky shell commands in this folder are protected by Approval Line, a hook that asks a human
to approve by phone before the command runs.

- **If Approval Line blocks a command, stop and ask the user.** Tell them the reason and the
  approval request ID from the block message, then wait for their instructions.
- **Do not retry a blocked command.** Every retry phones the approver again.
- **Do not look for a workaround.** Don't run an alternative command, a different script, a
  different shell, `bash -c`, an alias, or anything else that achieves the same result.
- **A block is a human decision or a safety stop, not an error to fix.** "No decision before
  the timeout" and "unreachable" also mean: stop and ask the user.
- **Don't edit the guard configuration** (`.claude/settings.json`, `guard-rules*.json`, the
  hook or its environment) to make a command pass.
- **Approval covers one exact command.** If the user wants to change the command, the new
  command needs its own approval.

To deploy, run exactly `./deploy.sh production` (Bash) or `./deploy.ps1 production`
(PowerShell). It is a harmless simulation that writes a record to `output/`.
