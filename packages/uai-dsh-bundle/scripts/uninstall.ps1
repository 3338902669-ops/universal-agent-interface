# 卸载 @uai/dsh-bundle（幂等；会备份 package.json）
param(
    [string]$Profile = "$env:USERPROFILE\.dsh\profiles\desktop",
    [switch]$WhatIf
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$node = (Get-Command node).Source

Write-Host "[1/2] 从 dsh.profile.bundles 里移除"
if (-not $WhatIf) {
    & $node (Join-Path $here 'scripts\patch-profile.mjs') remove --profile $Profile
    if ($LASTEXITCODE -ne 0) { throw "patch-profile failed with exit $LASTEXITCODE" }
}

Write-Host "[2/2] pnpm remove @uai/dsh-bundle"
if (-not $WhatIf) {
    Push-Location $Profile
    try {
        pnpm remove "@uai/dsh-bundle"
    } finally { Pop-Location }
}

Write-Host ""
Write-Host "卸载完成。重启 DSH 后左栏入口消失。"
