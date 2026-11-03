param(
    [ValidateRange(1, 65535)][int]$Port = 8767,
    [switch]$NoBrowser
)
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
$storeRoot = [IO.Path]::GetFullPath((Join-Path $projectRoot 'runs'))
$url = "http://127.0.0.1:$Port"

function Get-Workbench {
    try { return Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2 -ErrorAction Stop }
    catch { return $null }
}

$running = Get-Workbench
if ($running -and ($running.app -ne 'chuantuo' -or $running.store -ne $storeRoot)) {
    throw "Port $Port belongs to a different workspace. Choose another -Port."
}
if (-not $running) {
    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        throw "Install Python 3.12, then run: py -3.12 -m venv .venv and .\.venv\Scripts\python -m pip install -r requirements.lock.txt"
    }
    [IO.Directory]::CreateDirectory($storeRoot) | Out-Null
    $logBase = Join-Path $storeRoot ("workbench-{0}-{1}" -f (Get-Date -Format 'yyyyMMdd-HHmmss-fff'), $PID)
    $serverProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m','chuantuo','serve','--port',"$Port") -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput "$logBase.stdout.log" -RedirectStandardError "$logBase.stderr.log"
    for ($i=0; $i -lt 40; $i++) {
        if ($serverProcess.HasExited) { throw "Startup failed. See $logBase.stderr.log" }
        $running = Get-Workbench
        if ($running -and $running.app -eq 'chuantuo' -and $running.store -eq $storeRoot) { break }
        Start-Sleep -Milliseconds 300
    }
    if (-not $running -or $running.app -ne 'chuantuo' -or $running.store -ne $storeRoot) {
        throw "Workbench did not become ready. See $logBase.stderr.log"
    }
}
Write-Output "$url  (Chuantuo $($running.version))"
if (-not $NoBrowser) { Start-Process $url }
