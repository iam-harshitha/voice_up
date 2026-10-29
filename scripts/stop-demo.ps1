<#
.SYNOPSIS
  Stop ONLY the processes started by scripts/start-demo.ps1.

.DESCRIPTION
  Reads .demo/processes.json and stops each recorded PID together with its child
  processes (the venv launcher and npm both start children). Processes this script did
  not start - including other API servers or workers - are never touched. The database
  and logs are left as they are.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$StateFile = Join-Path $Root ".demo\processes.json"

if (-not (Test-Path $StateFile)) {
    Write-Host "Nothing to stop: no demo started by scripts\start-demo.ps1 is recorded."
    exit 0
}

$state = Get-Content $StateFile -Raw | ConvertFrom-Json
foreach ($entry in $state.PSObject.Properties) {
    $procId = [int]$entry.Value
    if (Get-Process -Id $procId -ErrorAction SilentlyContinue) {
        # /T stops the recorded process and its children only.
        & taskkill.exe /PID $procId /T /F | Out-Null
        Write-Host ("Stopped {0} (PID {1})" -f $entry.Name, $procId)
    } else {
        Write-Host ("{0} (PID {1}) was already stopped" -f $entry.Name, $procId)
    }
}
Remove-Item $StateFile
# The quick tunnel's URL dies with it; never let the doctor read a stale one.
$TunnelUrlFile = Join-Path $Root ".demo\tunnel-url.txt"
if (Test-Path $TunnelUrlFile) { Remove-Item $TunnelUrlFile }
Write-Host "Demo stopped. Logs remain in .demo\logs\."
