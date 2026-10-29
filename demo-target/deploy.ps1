# Harmless demo "deployment" for the Approval Line guard demo (PowerShell equivalent of
# deploy.sh). It deploys nothing: it only writes one JSON record to demo-target/output/.
param(
    [Parameter(Mandatory = $true)][ValidateSet("production", "staging")][string]$Target,
    [ValidatePattern("^[A-Za-z0-9._-]{1,40}$")][string]$Release = "v2.4"
)
$ErrorActionPreference = "Stop"

$outDir = Join-Path $PSScriptRoot "output"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$now = (Get-Date).ToUniversalTime()
$record = Join-Path $outDir ("deploy-{0}-{1}.json" -f $Target, $now.ToString("yyyyMMddTHHmmssZ"))
[ordered]@{
    simulated   = $true
    target      = $Target
    release     = $Release
    executed_at = $now.ToString("yyyy-MM-ddTHH:mm:ssZ")
    note        = "demo only: no real system was touched"
} | ConvertTo-Json | Set-Content -Path $record -Encoding utf8
Write-Output ("Simulated {0} deployment of {1} recorded in output/{2}" -f $Target, $Release, (Split-Path $record -Leaf))
