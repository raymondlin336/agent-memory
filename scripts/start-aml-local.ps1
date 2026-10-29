# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
# Local (no Docker) AML stack: MiniLM rerank server + AML Add/Search adapter in in-memory mode.
#
#   powershell -ExecutionPolicy Bypass -File scripts\start-aml-local.ps1          # start, wait until healthy
#   powershell -ExecutionPolicy Bypass -File scripts\start-aml-local.ps1 -Stop    # stop what it started
#
# Settings come from deploy\aml\.env (falls back to evaluation\environment\.env). The reranker is
# scripts\rerank_server.py on the port of RERANK_BASE_URL, run with the same python as the adapter
# (needs pip install -e ".[rerank]"). RERANK_SERVER_PYTHON / RERANK_SERVER_SCRIPT override that.
# If the reranker already answers, it is left alone.
# Logs, pids and the adapter ledger go to evaluation\outputs\aml_local\ (git-ignored).
# Kept ASCII-only so Windows PowerShell 5.1 reads it correctly without a BOM.
param(
    [switch]$Stop,
    [string]$EnvFile = "",
    [int]$Port = 8200,
    [string]$BindHost = "127.0.0.1",
    [string]$Python = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $Root "evaluation\outputs\aml_local"
$PidFile = Join-Path $RunDir "pids.json"
New-Item -ItemType Directory -Force $RunDir | Out-Null

function Stop-Started {
    if (-not (Test-Path $PidFile)) { Write-Host "nothing recorded in $PidFile"; return }
    $pids = Get-Content $PidFile -Raw | ConvertFrom-Json
    foreach ($p in $pids.PSObject.Properties) {
        # /T: the rerank launcher is a supervisor with a child process
        & taskkill.exe /PID $p.Value /T /F 2>$null | Out-Null
        Write-Host "stopped $($p.Name) (pid $($p.Value))"
    }
    Remove-Item $PidFile
}

function Test-Up([string]$Url) {
    try { Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 $Url | Out-Null; return $true } catch { return $false }
}

function Wait-Up([string]$Name, [string]$Url, [int]$Seconds, $Proc, [string]$Log) {
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-Up $Url) { Write-Host "$Name up: $Url"; return }
        if ($Proc -and $Proc.HasExited) { break }
        Start-Sleep -Seconds 2
    }
    Write-Host "$Name did not come up; last log lines from ${Log}:"
    if (Test-Path $Log) { Get-Content $Log -Tail 20 }
    throw "$Name failed to start"
}

if ($Stop) { Stop-Started; return }

if (-not $EnvFile) {
    $EnvFile = Join-Path $Root "deploy\aml\.env"
    if (-not (Test-Path $EnvFile)) { $EnvFile = Join-Path $Root "evaluation\environment\.env" }
}
if (-not (Test-Path $EnvFile)) { throw "env file not found: $EnvFile (copy deploy\aml\.env.example to deploy\aml\.env)" }
foreach ($line in Get-Content $EnvFile) {
    $t = $line.Trim()
    if (-not $t -or $t.StartsWith("#") -or -not $t.Contains("=")) { continue }
    $k, $v = $t.Split("=", 2)
    $v = $v.Trim().Trim('"').Trim("'")
    if (-not [Environment]::GetEnvironmentVariable($k.Trim())) { Set-Item "env:$($k.Trim())" $v }
}
# The key is required on every address; the adapter refuses empty, short or placeholder keys.
if (-not $env:AML_ADAPTER_KEY) { throw "AML_ADAPTER_KEY is not set in $EnvFile (32+ random characters; see deploy\aml\README.md)" }
if (-not $env:AML_IN_MEMORY) { $env:AML_IN_MEMORY = "1" }
if (-not $env:AML_STATE_DB) { $env:AML_STATE_DB = Join-Path $RunDir "aml_adapter_state.db" }
$env:PYTHONIOENCODING = "utf-8"
$env:PYTHONPATH = $Root
if (-not $Python) { $Python = Join-Path $Root ".venv\Scripts\python.exe" }
if (-not (Test-Path $Python)) { throw "python not found: $Python (create .venv and pip install -e "".[dev]"")" }

$pids = @{}
if (-not $env:RERANK_BASE_URL) { $env:RERANK_BASE_URL = "http://127.0.0.1:8906/v1" }
if (-not $env:RERANK_API_KEY) { $env:RERANK_API_KEY = "local" }
$rerankUrl = $env:RERANK_BASE_URL.TrimEnd("/")
if (Test-Up $rerankUrl) {
    Write-Host "reranker already up: $rerankUrl"
} else {
    $rerankPython = if ($env:RERANK_SERVER_PYTHON) { $env:RERANK_SERVER_PYTHON } else { $Python }
    $rerankScript = if ($env:RERANK_SERVER_SCRIPT) { $env:RERANK_SERVER_SCRIPT } else { Join-Path $Root "scripts\rerank_server.py" }
    $rerankPort = ([Uri]$rerankUrl).Port
    $log = Join-Path $RunDir "reranker.log"
    $p = Start-Process -FilePath $rerankPython -ArgumentList "`"$rerankScript`"", "--port", $rerankPort `
        -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
    $pids.reranker = $p.Id
    $pids | ConvertTo-Json | Set-Content $PidFile
    # First start downloads the model (~90 MB), hence the long wait.
    Wait-Up "reranker" $rerankUrl 600 $p "$log.err"
}

$adapterUrl = "http://${BindHost}:$Port"
if (Test-Up "$adapterUrl/health") { throw "something already answers on $adapterUrl; stop it first (-Stop)" }
$log = Join-Path $RunDir "adapter.log"
$p = Start-Process -FilePath $Python `
    -ArgumentList "`"$Root\scripts\aml_adapter.py`"", "--config", "`"$Root\deploy\aml\config.yml`"", "--host", $BindHost, "--port", $Port `
    -WorkingDirectory $Root -RedirectStandardOutput $log -RedirectStandardError "$log.err" -WindowStyle Hidden -PassThru
$pids.adapter = $p.Id
$pids | ConvertTo-Json | Set-Content $PidFile
Wait-Up "adapter" "$adapterUrl/health" 180 $p "$log.err"
Write-Host "ready: POST $adapterUrl/add and $adapterUrl/search (in-memory=$($env:AML_IN_MEMORY)); logs in $RunDir"
