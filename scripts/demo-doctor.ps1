<#
.SYNOPSIS
  Read-only preflight for the live Claude Code + Approval Line + Bolna demo.

.DESCRIPTION
  Prints PASS / WARN / FAIL / SKIP for each check and a final verdict. Exit code 0 means
  READY, 1 means NOT READY. It never prints secrets (API keys, tokens and phone numbers are
  checked, never shown), places no phone calls and writes nothing. Database queries run in a
  READ ONLY transaction. HTTP requests are GETs, plus one token-less POST to the public
  webhook, which is refused (401) before anything is stored.

  Run it in the SAME PowerShell window you start Claude Code from: it checks the
  APPROVAL_LINE_* variables that the hook will inherit.

  Checks: Claude Code version (>= 2.1.295 for the hook's onFailure), the hook registration
  and its Python, the rules file and its policies (auto-dial on, active approvers with
  phone numbers, same organization as the key), migrations at head, API readiness,
  VOICE_PROVIDER and the Bolna settings (names only), the voice worker (process plus
  overdue-job signal), the agent key's scopes, the public tunnel and webhook route, and the
  Bolna agent's saved webhook URL and token header (one read-only GET; -NoBolna skips it).

.EXAMPLE
  .\scripts\demo-doctor.ps1
  .\scripts\demo-doctor.ps1 -NoBolna
#>
[CmdletBinding()]
param(
    [switch]$NoBolna
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root "env\Scripts\python.exe"
$Doctor = Join-Path $Root "backend\scripts\demo_doctor.py"

if (-not (Test-Path $Python)) {
    Write-Host "[FAIL] Virtualenv not found at env\. Create it and run: pip install -e `".[dev]`" in backend\." -ForegroundColor Red
    exit 1
}

$arguments = @($Doctor)
if ($NoBolna) { $arguments += "--no-bolna" }

# Windows PowerShell 5.1 turns a native command's stderr into errors under "Stop"; the
# doctor writes its report to stdout, so run it with "Continue" and keep its exit code.
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    & $Python @arguments 2>$null | ForEach-Object {
        $color = switch -Regex ($_) {
            "^\[PASS\]" { "Green" }
            "^\[FAIL\]" { "Red" }
            "^\[WARN\]" { "Yellow" }
            "^Verdict: READY" { "Green" }
            "^Verdict: NOT READY" { "Red" }
            default { "Gray" }
        }
        Write-Host $_ -ForegroundColor $color
    }
    $code = $LASTEXITCODE
} finally {
    $ErrorActionPreference = $previous
}
exit $code
