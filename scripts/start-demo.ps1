<#
.SYNOPSIS
  Start the Approval Line local demo stack: API, worker and dashboard.

.DESCRIPTION
  Local development only (refuses APP_ENV=production). It:
    - checks prerequisites (virtualenv, backend/.env, frontend/node_modules);
    - reports configuration as PRESENT/ABSENT only (never prints values);
    - refuses to start if a port is already in use, or if a worker is already running
      (use -SkipWorker to keep using the existing worker);
    - checks the migration state (-Migrate applies `alembic upgrade head`, which is
      additive; nothing is ever dropped or reset);
    - starts the processes hidden, logs to .demo/logs, and records ONLY their PIDs in
      .demo/processes.json so scripts/stop-demo.ps1 stops exactly those and nothing else;
    - waits for the API readiness endpoint and the dashboard;
    - with VOICE_PROVIDER=bolna, starts a Cloudflare quick tunnel to the API (unless
      -NoTunnel), waits until it serves /health/ready, saves its URL to
      .demo/tunnel-url.txt (read by scripts/demo-doctor.ps1) and prints the webhook URL to
      paste into Bolna. A quick tunnel gets a NEW URL every time it starts.

  The MCP server and demo agent are started per run (see backend/docs/demo-runbook.md).
  Run scripts\demo-doctor.ps1 afterwards for a read-only go/no-go check.

.EXAMPLE
  .\scripts\start-demo.ps1 -Migrate
  .\scripts\start-demo.ps1 -ApiPort 8001 -WebPort 5174 -SkipWorker
  .\scripts\start-demo.ps1 -NoTunnel
#>
[CmdletBinding()]
param(
    [int]$ApiPort = 8000,
    [int]$WebPort = 5173,
    [switch]$Migrate,
    [switch]$SkipWorker,
    [switch]$NoTunnel
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$Backend = Join-Path $Root "backend"
$Frontend = Join-Path $Root "frontend"
$Python = Join-Path $Root "env\Scripts\python.exe"
$StateDir = Join-Path $Root ".demo"
$LogDir = Join-Path $StateDir "logs"
$StateFile = Join-Path $StateDir "processes.json"
$TunnelUrlFile = Join-Path $StateDir "tunnel-url.txt"

function Fail([string]$Message) {
    Write-Host "ERROR: $Message" -ForegroundColor Red
    exit 1
}

# Windows PowerShell 5.1 turns a native command's stderr into terminating errors when
# $ErrorActionPreference is Stop; alembic logs to stderr, so run it with Continue.
function Invoke-Python([string[]]$Arguments, [switch]$Quiet) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        if ($Quiet) { & $Python @Arguments 2>$null } else { & $Python @Arguments 2>&1 | ForEach-Object { "$_" } }
    } finally {
        $ErrorActionPreference = $previous
    }
}

# --- Prerequisites ---------------------------------------------------------------------
if (-not (Test-Path $Python)) { Fail "Virtualenv not found at env\. Create it and run: pip install -e `".[dev]`" in backend\." }
if (-not (Test-Path (Join-Path $Backend ".env"))) { Fail "backend\.env is missing. Copy backend\.env.example to backend\.env and fill it in." }
if (-not (Test-Path (Join-Path $Frontend "node_modules"))) { Fail "frontend\node_modules is missing. Run: cd frontend; npm ci" }
if (-not (Get-Command npm.cmd -ErrorAction SilentlyContinue)) { Fail "npm is not on PATH (install Node.js 20+)." }
if (Test-Path $StateFile) { Fail "A demo started by this script may still be running (.demo\processes.json exists). Run scripts\stop-demo.ps1 first." }

# --- Configuration (presence only, never values) -----------------------------------------
Push-Location $Backend
try {
    $configJson = & $Python -c @"
import json
from app.core.config import get_settings
s = get_settings()
tok = s.voice_webhook_token
print(json.dumps({
  'app_env': s.app_env,
  'voice_provider': s.voice_provider,
  'webhook_token': bool(tok and tok.get_secret_value()),
  'mock_state_dir': bool(s.voice_mock_state_dir),
  'bolna_key': bool(s.bolna_api_key and s.bolna_api_key.get_secret_value()),
}))
"@
    if ($LASTEXITCODE -ne 0) { Fail "backend\.env could not be loaded (see the error above; values are not printed)." }
} finally { Pop-Location }
$config = $configJson | ConvertFrom-Json
if ($config.app_env -eq "production") { Fail "APP_ENV=production: this script is for local demos only." }

Write-Host "Configuration (values are never shown):"
Write-Host ("  APP_ENV               {0}" -f $config.app_env)
Write-Host ("  VOICE_PROVIDER        {0}" -f $config.voice_provider)
Write-Host ("  VOICE_WEBHOOK_TOKEN   {0}" -f $(if ($config.webhook_token) { "PRESENT" } else { "ABSENT" }))
Write-Host ("  VOICE_MOCK_STATE_DIR  {0}" -f $(if ($config.mock_state_dir) { "PRESENT" } else { "ABSENT" }))
Write-Host ("  BOLNA_API_KEY         {0}" -f $(if ($config.bolna_key) { "PRESENT" } else { "ABSENT" }))
switch ($config.voice_provider) {
    "mock" {
        Write-Host "  >> SIMULATED VOICE: the mock provider places no real phone calls." -ForegroundColor Yellow
        if (-not $config.webhook_token -or -not $config.mock_state_dir) {
            Write-Host "  >> WARNING: voice-simulate needs VOICE_WEBHOOK_TOKEN and VOICE_MOCK_STATE_DIR in backend\.env." -ForegroundColor Yellow
        }
    }
    "bolna" {
        Write-Host "  >> REAL VOICE: Bolna places real phone calls to the approvers' numbers." -ForegroundColor Magenta
        Write-Host "     One live approval call was verified on 2026-10-28; other paths are mock-verified. Run scripts\demo-doctor.ps1 before presenting." -ForegroundColor Magenta
    }
    default { Write-Host "  >> Voice calls disabled." }
}

# --- Ports and duplicate workers -----------------------------------------------------------
foreach ($port in @($ApiPort, $WebPort)) {
    $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
        Fail "Port $port is already in use (process $($listener.OwningProcess) $owner). Stop it or choose another port (-ApiPort / -WebPort)."
    }
}
$existingWorkers = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
    Where-Object { $_.CommandLine -match "app\.workers\.voice_worker" })
if ($existingWorkers.Count -gt 0 -and -not $SkipWorker) {
    Fail "A voice worker is already running (PID $($existingWorkers[0].ProcessId)). Use -SkipWorker to reuse it, or stop it first."
}

# --- Migrations ------------------------------------------------------------------------------
Push-Location $Backend
try {
    if ($Migrate) {
        Write-Host "Applying migrations (alembic upgrade head; additive, never destructive)..."
        Invoke-Python @("-m", "alembic", "upgrade", "head") | Write-Host
        if ($LASTEXITCODE -ne 0) { Fail "alembic upgrade head failed." }
    }
    $current = (Invoke-Python @("-m", "alembic", "current") -Quiet | Select-Object -Last 1)
    $head = (Invoke-Python @("-m", "alembic", "heads") -Quiet | Select-Object -Last 1)
    if (-not $current -or -not $head -or -not $current.StartsWith(($head -split " ")[0])) {
        Fail "Database is not at the latest migration (current: '$current', head: '$head'). Re-run with -Migrate."
    }
    Write-Host "Database migration: $current"
} finally { Pop-Location }

# --- Start ---------------------------------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$started = [ordered]@{}

$api = Start-Process -FilePath $Python -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
    -ArgumentList @("-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", "$ApiPort") `
    -RedirectStandardOutput (Join-Path $LogDir "api.out.log") -RedirectStandardError (Join-Path $LogDir "api.err.log")
$started["api"] = $api.Id

if (-not $SkipWorker) {
    $worker = Start-Process -FilePath $Python -WorkingDirectory $Backend -WindowStyle Hidden -PassThru `
        -ArgumentList @("-m", "app.workers.voice_worker", "--worker-id", "demo-$env:COMPUTERNAME") `
        -RedirectStandardOutput (Join-Path $LogDir "worker.out.log") -RedirectStandardError (Join-Path $LogDir "worker.err.log")
    $started["worker"] = $worker.Id
}

$env:APPROVAL_LINE_BACKEND_URL = "http://127.0.0.1:$ApiPort"
$web = Start-Process -FilePath "npm.cmd" -WorkingDirectory $Frontend -WindowStyle Hidden -PassThru `
    -ArgumentList @("run", "dev", "--", "--host", "127.0.0.1", "--port", "$WebPort", "--strictPort") `
    -RedirectStandardOutput (Join-Path $LogDir "web.out.log") -RedirectStandardError (Join-Path $LogDir "web.err.log")
$started["web"] = $web.Id

$started | ConvertTo-Json | Set-Content -Encoding utf8 $StateFile

# --- Readiness -----------------------------------------------------------------------------------
function Wait-Url([string]$Url, [int]$Seconds) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        try {
            $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $true }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

if (-not (Wait-Url "http://127.0.0.1:$ApiPort/api/v1/health/ready" 60)) {
    Write-Host "API did not become ready; see .demo\logs\api.err.log. Stopping what was started." -ForegroundColor Red
    & (Join-Path $PSScriptRoot "stop-demo.ps1")
    exit 1
}
if (-not (Wait-Url "http://127.0.0.1:$WebPort/" 90)) {
    Write-Host "Dashboard did not start; see .demo\logs\web.err.log. Stopping what was started." -ForegroundColor Red
    & (Join-Path $PSScriptRoot "stop-demo.ps1")
    exit 1
}

# --- Public tunnel for Bolna's webhooks ----------------------------------------------------------
$tunnelUrl = $null
if ($config.voice_provider -eq "bolna" -and -not $NoTunnel) {
    $cloudflared = (Get-Command cloudflared -ErrorAction SilentlyContinue).Source
    if (-not $cloudflared) {
        foreach ($candidate in @("${env:ProgramFiles(x86)}\cloudflared\cloudflared.exe", "$env:ProgramFiles\cloudflared\cloudflared.exe")) {
            if ($candidate -and (Test-Path $candidate)) { $cloudflared = $candidate; break }
        }
    }
    if (-not $cloudflared) {
        Write-Host "WARNING: cloudflared not found; Bolna cannot reach this API. Install it or start your own tunnel (-NoTunnel)." -ForegroundColor Yellow
    } else {
        $others = @(Get-Process cloudflared -ErrorAction SilentlyContinue)
        if ($others.Count -gt 0) {
            Write-Host ("NOTE: {0} cloudflared process(es) were already running (PID {1}); they are left alone. Bolna must use the URL printed below." -f $others.Count, ($others.Id -join ", ")) -ForegroundColor Yellow
        }
        $tunnelLog = Join-Path $LogDir "tunnel.err.log"
        $tunnel = Start-Process -FilePath $cloudflared -WindowStyle Hidden -PassThru `
            -ArgumentList @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$ApiPort") `
            -RedirectStandardOutput (Join-Path $LogDir "tunnel.out.log") -RedirectStandardError $tunnelLog
        $started["tunnel"] = $tunnel.Id
        $started | ConvertTo-Json | Set-Content -Encoding utf8 $StateFile

        $deadline = (Get-Date).AddSeconds(45)
        while (-not $tunnelUrl -and (Get-Date) -lt $deadline) {
            Start-Sleep -Milliseconds 500
            if (Test-Path $tunnelLog) {
                # Not api.trycloudflare.com, which appears in cloudflared's own error lines.
                $match = Select-String -Path $tunnelLog -Pattern "https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com" -ErrorAction SilentlyContinue | Select-Object -First 1
                if ($match) { $tunnelUrl = $match.Matches[0].Value }
            }
        }
        if (-not $tunnelUrl) {
            Write-Host "WARNING: the tunnel did not report a URL within 45 s; see .demo\logs\tunnel.err.log." -ForegroundColor Yellow
        } elseif (-not (Wait-Url "$tunnelUrl/api/v1/health/ready" 60)) {
            Write-Host "WARNING: $tunnelUrl does not serve the API yet (DNS can take a minute). Re-check with scripts\demo-doctor.ps1." -ForegroundColor Yellow
            Set-Content -Encoding utf8 -NoNewline -Path $TunnelUrlFile -Value $tunnelUrl
        } else {
            Set-Content -Encoding utf8 -NoNewline -Path $TunnelUrlFile -Value $tunnelUrl
        }
    }
}

Write-Host ""
Write-Host "Approval Line demo is running:" -ForegroundColor Green
Write-Host "  API        http://127.0.0.1:$ApiPort/api/v1/health/ready   (PID $($started['api']))"
if ($started.Contains("worker")) { Write-Host "  Worker     PID $($started['worker'])" } else { Write-Host "  Worker     reusing the already-running worker" }
Write-Host "  Dashboard  http://127.0.0.1:$WebPort/   (PID $($started['web']))"
if ($started.Contains("tunnel")) { Write-Host "  Tunnel     $(if ($tunnelUrl) { $tunnelUrl } else { '(no URL yet)' })   (PID $($started['tunnel']))" }
Write-Host "  Logs       .demo\logs\"
if ($tunnelUrl) {
    Write-Host ""
    Write-Host "Paste this webhook URL into Bolna (agent > Extractions > Webhook URL), then Save agent:" -ForegroundColor Cyan
    Write-Host "  $tunnelUrl/api/v1/voice/webhooks/bolna" -ForegroundColor Cyan
    Write-Host "  (A quick tunnel's URL changes on every start; the header X-Approval-Line-Webhook-Token stays the same.)"
}
Write-Host "Check readiness:  .\scripts\demo-doctor.ps1"
Write-Host "Stop with:  .\scripts\stop-demo.ps1"
