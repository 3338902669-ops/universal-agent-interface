# 一键安装 @uai/dsh-bundle 到当前 DSH profile（幂等；会备份 package.json）
# 用法：  .\install.ps1 [-Profile <profile 目录>] [-CoreCmd "<python> -m uai_core"] [-WhatIf]
param(
    [string]$Profile = "$env:USERPROFILE\.dsh\profiles\desktop",
    [string]$CoreCmd = "",
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$node = (Get-Command node).Source
if (-not $node) { throw "node not found on PATH" }

Write-Host "[1/3] pnpm add link:$here"
if (-not $WhatIf) {
    Push-Location $Profile
    try {
        pnpm add "link:$here"
        if ($LASTEXITCODE -ne 0) { throw "pnpm add failed with exit $LASTEXITCODE" }
    } finally { Pop-Location }
}

Write-Host "[2/3] 把 @uai/dsh-bundle 写进 dsh.profile.bundles"
if (-not $WhatIf) {
    & $node (Join-Path $here 'scripts\patch-profile.mjs') add --profile $Profile
    if ($LASTEXITCODE -ne 0) { throw "patch-profile failed with exit $LASTEXITCODE" }
}

Write-Host "[3/3] 环境变量提示"
if ($CoreCmd) {
    if (-not $WhatIf) { setx UAI_CMD $CoreCmd | Out-Null }
    Write-Host "  已设置 UAI_CMD=$CoreCmd（新终端生效）"
} else {
    Write-Host "  未设置 UAI_CMD；插件默认调 PATH 上的 uai。若要指定核心："
    Write-Host "    setx UAI_CMD \"<python.exe> -m uai_core\""
}

Write-Host ""
Write-Host "安装完成。重启 DSH 桌面版后，左侧栏会出现「UAI 协作」。"
Write-Host "验证：重启后新建会话，让助手调一次 uai_status，思考链里应出现 UAI 卡片。"
