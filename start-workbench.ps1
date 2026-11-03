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

$explicitPort = $PSBoundParameters.ContainsKey('Port')
$portFile = Join-Path $storeRoot '.workbench-port'
if (-not $explicitPort -and (Test-Path -LiteralPath $portFile -PathType Leaf)) {
    $priorPort = 0
    if ([int]::TryParse(([IO.File]::ReadAllText($portFile)).Trim(), [ref]$priorPort) -and $priorPort -ge 1 -and $priorPort -le 65535) {
        $url = "http://127.0.0.1:$priorPort"
        $priorService = Get-Workbench
        if ($priorService.app -eq 'chuantuo' -and $priorService.store -eq $storeRoot) { $Port = $priorPort }
    }
}
# Keep a previous workspace on its own port when two checkouts are open.
$running = $null
for ($candidate = $Port; $candidate -le [Math]::Min($Port + 20, 65535); $candidate++) {
    $url = "http://127.0.0.1:$candidate"
    $running = Get-Workbench
    if ($running.app -eq 'chuantuo' -and $running.store -eq $storeRoot) { break }
    $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $candidate)
    try { $probe.Start(); $running = $null; break }
    catch [Net.Sockets.SocketException] {
        if ($explicitPort) { throw "Port $Port is in use. Choose a different -Port." }
    }
    finally { $probe.Stop() }
}
if ($candidate -gt [Math]::Min($Port + 20, 65535)) { throw "No local port available. Choose a different -Port." }
$Port = $candidate
if (-not $running) {
    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
        $launcher = Get-Command py -ErrorAction SilentlyContinue
        if ($launcher) { & $launcher.Source -3.12 -m venv (Join-Path $projectRoot '.venv') }
        if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
            $systemPython = Get-Command python -ErrorAction SilentlyContinue
            if ($systemPython) {
                & $systemPython.Source -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))"
                if ($LASTEXITCODE -eq 0) { & $systemPython.Source -m venv (Join-Path $projectRoot '.venv') }
            }
        }
        if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "Install Python 3.12 and start Trantor again." }
    }
    & $pythonExe -c "from importlib.metadata import distributions; import sys,tomllib; from pathlib import Path; sys.exit(next((d.version for d in distributions(name='chuantuo')),None)!=tomllib.loads(Path(sys.argv[1]).read_text('utf-8'))['project']['version'])" (Join-Path $projectRoot 'pyproject.toml')
    if ($LASTEXITCODE -ne 0) {
        Write-Output "Installing Trantor and its dependencies for this workspace..."
        & $pythonExe -m pip install --disable-pip-version-check -c (Join-Path $projectRoot 'requirements.lock.txt') -e $projectRoot
        if ($LASTEXITCODE -ne 0) { throw "Dependency installation did not finish. Run this launcher again to retry." }
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
[IO.File]::WriteAllText($portFile, [string]$Port)
Write-Output "$url  (Trantor $($running.version))"
if (-not $NoBrowser) { Start-Process $url }
