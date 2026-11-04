param(
  [switch]$ResetData
)

$ErrorActionPreference = 'Stop'
$base = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pidFile = Join-Path $base 'runtime\pids.json'
$portsFile = Join-Path $base 'runtime\ports.json'

if ($ResetData) {
  throw 'ResetData is intentionally unsupported; lab evidence is append-only.'
}

if (Test-Path -LiteralPath $pidFile) {
  try {
    $existing = Get-Content -LiteralPath $pidFile -Raw | ConvertFrom-Json
    $natsProcess = Get-Process -Id $existing.nats_pid -ErrorAction SilentlyContinue
    $hubProcess = Get-Process -Id $existing.hub_pid -ErrorAction SilentlyContinue
    if ($natsProcess -and $hubProcess) {
      Write-Output "lab already running: nats_pid=$($existing.nats_pid) hub_pid=$($existing.hub_pid)"
      exit 0
    }
  } catch {}
}

foreach ($port in @(43331, 43332)) {
  $listener = Get-NetTCPConnection -State Listen -LocalPort $port -ErrorAction SilentlyContinue
  if ($listener) {
    throw "lab port $port is already occupied by PID $($listener.OwningProcess)"
  }
}

$natsCommand = if ($env:NATS_SERVER) {
  Get-Command $env:NATS_SERVER -ErrorAction Stop
} else {
  Get-Command nats-server -ErrorAction Stop
}
$natsExe = $natsCommand.Source
$config = Join-Path $base 'nats-test.conf'
$pythonCommand = if ($env:PYTHON) {
  Get-Command $env:PYTHON -ErrorAction Stop
} else {
  $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
  if ($pyLauncher) {
    $pyLauncher
  } else {
    Get-Command python -ErrorAction Stop
  }
}
if ($pythonCommand.Name -match '^py(\.exe)?$') {
  $python = (& $pythonCommand.Source -3 -c "import sys; print(sys.executable)").Trim()
  if ($LASTEXITCODE -ne 0 -or -not $python) {
    throw 'failed to resolve the Python 3 interpreter through py.exe'
  }
} else {
  $python = $pythonCommand.Source
}
$natsOut = Join-Path $base 'logs\nats-stdout.log'
$natsErr = Join-Path $base 'logs\nats-stderr.log'
$hubOut = Join-Path $base 'logs\hub-stdout.log'
$hubErr = Join-Path $base 'logs\hub-stderr.log'

New-Item -ItemType Directory -Path (Join-Path $base 'logs'), (Join-Path $base 'runtime') -Force | Out-Null

$nats = Start-Process -FilePath $natsExe -ArgumentList @('-c', $config) `
  -WorkingDirectory $base -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput $natsOut -RedirectStandardError $natsErr

$ready = $false
for ($i = 0; $i -lt 40; $i++) {
  Start-Sleep -Milliseconds 250
  try {
    $varz = Invoke-RestMethod -Uri 'http://127.0.0.1:43332/varz' -TimeoutSec 1
    if ($varz.server_id) { $ready = $true; break }
  } catch {}
}
if (-not $ready) {
  Stop-Process -Id $nats.Id -Force -ErrorAction SilentlyContinue
  throw 'independent NATS instance did not become ready'
}

$hub = Start-Process -FilePath $python `
  -ArgumentList @((Join-Path $base 'hub.py'), '--base', $base, '--nats-url', 'nats://127.0.0.1:43331', '--lease-seconds', '300') `
  -WorkingDirectory $base -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput $hubOut -RedirectStandardError $hubErr

$hubReady = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Milliseconds 250
  if ($hub.HasExited) {
    $tail = if (Test-Path -LiteralPath $hubErr) { Get-Content -LiteralPath $hubErr -Tail 20 | Out-String } else { '' }
    Stop-Process -Id $nats.Id -Force -ErrorAction SilentlyContinue
    throw "hub exited early; stderr=$tail"
  }
  if ((Test-Path -LiteralPath $hubOut) -and ((Get-Content -LiteralPath $hubOut -Raw) -match 'hub_ready')) {
    $hubReady = $true
    break
  }
}
if (-not $hubReady) {
  Stop-Process -Id $hub.Id -Force -ErrorAction SilentlyContinue
  Stop-Process -Id $nats.Id -Force -ErrorAction SilentlyContinue
  throw 'hub did not become ready'
}

@{
  nats_pid = $nats.Id
  hub_pid = $hub.Id
  nats_config = $config
} | ConvertTo-Json | Set-Content -LiteralPath $pidFile -Encoding utf8
@{
  nats = '127.0.0.1:43331'
  monitor = '127.0.0.1:43332'
  collision_basis = 'checked before start; production 4222/8222 and other lab ports avoided'
} | ConvertTo-Json | Set-Content -LiteralPath $portsFile -Encoding utf8

Write-Output "lab ready: nats_pid=$($nats.Id) hub_pid=$($hub.Id) nats=127.0.0.1:43331 monitor=127.0.0.1:43332"
