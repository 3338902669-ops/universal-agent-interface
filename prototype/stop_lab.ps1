$ErrorActionPreference = 'Stop'
$base = [System.IO.Path]::GetFullPath($PSScriptRoot)
$pidFile = Join-Path $base 'runtime\pids.json'
if (-not (Test-Path -LiteralPath $pidFile)) {
  Write-Output 'no lab pid file'
  exit 0
}
$targets = Get-CimInstance Win32_Process | Where-Object {
  $_.Name -in @('python.exe', 'nats-server.exe') -and
  $_.CommandLine -like "*$base*"
} | Sort-Object { if ($_.Name -eq 'python.exe') { 0 } else { 1 } }
foreach ($process in $targets) {
  Stop-Process -Id $process.ProcessId -Force -ErrorAction SilentlyContinue
  Write-Output "stopped $($process.Name) pid=$($process.ProcessId)"
}
