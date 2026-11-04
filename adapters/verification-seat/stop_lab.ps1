$ErrorActionPreference = 'Stop'
$base = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pidFile = Join-Path $base 'runtime\pids.json'
$evidenceDir = Join-Path $base 'evidence'
$shutdownPath = Join-Path $evidenceDir 'shutdown.json'
New-Item -ItemType Directory -Path $evidenceDir -Force | Out-Null

$before = @(
  Get-CimInstance Win32_Process |
    Where-Object {
      $_.Name -in @('python.exe', 'nats-server.exe') -and
      $_.CommandLine -like "*$base*"
    } |
    Select-Object ProcessId, ParentProcessId, Name, CommandLine
)
$stopOutput = @()
if (-not (Test-Path -LiteralPath $pidFile)) {
  $stopOutput += 'no lab pid file'
} else {
  $targets = $before |
    Sort-Object { if ($_.Name -eq 'python.exe') { 0 } else { 1 } }
  foreach ($process in $targets) {
    Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
    $stopOutput += "stopped $($process.Name) pid=$($process.ProcessId)"
  }
}

Start-Sleep -Milliseconds 300
$after = @(
  Get-CimInstance Win32_Process |
    Where-Object {
      $_.Name -in @('python.exe', 'nats-server.exe') -and
      $_.CommandLine -like "*$base*"
    } |
    Select-Object ProcessId, ParentProcessId, Name, CommandLine
)
$listenerLines = @(
  netstat -ano -p tcp |
    Select-String -Pattern ':(43331|43332)\s+.*LISTENING'
)

@{
  checked_at = (Get-Date).ToString('o')
  lab_base = $base
  before = $before
  stop_output = ($stopOutput -join "`r`n")
  after = $after
  lab_listener_lines = @($listenerLines | ForEach-Object { $_.Line.Trim() })
  production_paths_touched_by_stop = $false
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $shutdownPath -Encoding utf8

$stopOutput | ForEach-Object { Write-Output $_ }
Write-Output "shutdown evidence: $shutdownPath"
