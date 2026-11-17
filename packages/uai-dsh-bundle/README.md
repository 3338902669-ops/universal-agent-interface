# @uai/dsh-bundle

DSH 里的 UAI 协作面板：左侧栏一个「UAI 协作」页，显示中枢/席位状态、一键检测、一键刷新。
后端是 UAI 核心（uai_core）；本包只做**桥接与渲染**，不复制任何协议逻辑。

## 结构

| 文件 | 作用 |
|---|---|
| lib/index.js | 宿主侧插件：在 127.0.0.1:47311 开只读 HTTP 桥，把 uai status/detect/seats 转成 JSON（含 CORS） |
| cordis.patch.yml | bundle 的插件行声明（DSH 按 `dsh.profile.bundles` → 每个 bundle 的 patch → profile patch 的顺序合成） |
| lib/client.js | 客户端插件：注册 sidebar.panellist 入口 + main 面板内容（window.__ModuleLoader__ 格式，React 由宿主提供） |
| test/host-selftest.mjs | 不装进 DSH 也能验：桥启动、CORS、status JSON、404 |
| test/tool-selftest.mjs | 验两个工具 uai_status / uai_detect：注册、参数表、输出 schema、execute 摘要与字段 |
| test/client-selftest.mjs | 用 React 垫片 + loader mock 验客户端：注册 4 个 slot、面板与两张卡片都能渲染 |
| scripts/install.ps1 | 一键安装（pnpm link + 写 bundles + 环境变量提示），幂等，改前备份 |
| scripts/uninstall.ps1 | 一键卸载（移出 bundles + pnpm remove） |
| scripts/patch-profile.mjs | 幂等维护 profile 的 dsh.profile.bundles（add/remove/check） |
| scripts/compose-check.mjs | **静态**验证「DSH 会不会加载它」：按合成顺序把 patch 应用一遍，检查 uai-collab 行是否进树 |

宿主注册两个工具：**uai_status**（常驻面：中枢/席位/监督）与 **uai_detect**（能力矩阵：就绪 N/M）。模型可以直接问，
客户端为它注册 `tool.call.toolview` 卡片 —— 这样一次调用在思考链里是一张卡（中枢/席位/监督 + 状态色），
而不是一行 JSON。卡片各一张：`uai_status` → 中枢/席位/监督三格；`uai_detect` → 摘要 + 就绪 agent 标签。

端点：/uai/ping、/uai/status、/uai/detect、/uai/seats。

## 依赖

- UAI 核心已安装：pip install -e packages/uai-core（或任意 uai 在 PATH 上）
- 插件通过环境变量 UAI_CMD 找核心，默认 uai；建议显式设成 "<python> -m uai_core"
- UAI_BASE 指向数据面（团队 base 或 ~/.uai/base）
- 桥端口可用 UAI_BRIDGE_PORT 覆盖（默认 47311）

## 安装（改 profile，需要重启 DSH 才生效）

~~~powershell
# 一键（幂等、改前自动备份 package.json）
.\scripts\install.ps1 -CoreCmd "<python.exe> -m uai_core"

# 等价的手工步骤：
# 1) 装进当前 profile（link 方式：改代码后只需重启，不用重装）
cd $env:USERPROFILE\.dsh\profiles\desktop
pnpm add "link:<repo>\packages\uai-dsh-bundle"

# 2) 让 profile 加载这个 bundle：在 package.json 的 dsh.profile.bundles 里追加 @uai/dsh-bundle
#    然后确认 cordis.patch.yml 里对应行没有 disabled

# 3) 指定核心命令与数据面（可选但推荐）
setx UAI_CMD "C:\path\to\python.exe -m uai_core"
setx UAI_BASE "C:\Users\33389\.shared\comms-v2\cutover-base-20261109"

# 4) 重启 DSH（桌面版退出再开）
~~~

重启后左侧栏出现「UAI 协作」。右上角两个按钮：**检测并搭建**（调 uai detect）、**刷新**（重读 uai status）。

## 自检（不需要重启）

~~~powershell
cd packages\uai-dsh-bundle
$env:UAI_CMD="<python> -m uai_core"; $env:UAI_BASE="<数据面>"
node test/host-selftest.mjs
# 期望 RESULT: 5/5 passed，且 /uai/status ok=true
node test/tool-selftest.mjs
# 期望 RESULT: 14/14 passed，摘要形如「中枢 运行中 · 席位 4/4 · 监督 1」「就绪 5/12（dsh, codex, …）」
node test/client-selftest.mjs
# 期望 RESULT: 11/11 passed（不需要 React、不需要 DSH）
~~~

## 卸载

~~~powershell
.\scripts\uninstall.ps1
~~~

## 已知边界（如实）

1. **面板还是只读视图**：能看、能触发检测，还不能从这里派单（派单走工具/MCP，P3 再接）；
2. **卡片只对 uai_status 生效**：其余 UAI 动作还没有卡片（等派单工具做出来再逐个加）；
3. **安装需要重启 DSH**：插件安装没有 HMR（restart-required），所以没有直接装进正在使用的 profile；
4. **桥没有鉴权**：只监听 127.0.0.1，同机进程可读；不暴露任何写操作（没有起停接口）。

## 安装后在 DSH 里发生什么（加载路径，已核过）

```
profile/cordis.yml []                      <- 空入口表
  ↑ 合成顺序：
  1) dsh.profile.bundles 里每个 bundle 的 dsh.bundle.patch
     → @uai/dsh-bundle/cordis.patch.yml 插入 { id: uai-collab, name: @uai/dsh-bundle, config.port }
  2) profile/cordis.patch.yml（用户的 patch 层，后应用，可覆盖/禁用）
  3) --patch 覆盖层
```

宿主插件启动后：监听 127.0.0.1:47311 + 注册 uai_status / uai_detect 两个工具；
客户端装载器按 package.json 的 `dsh.client.platform` 与 `exports["./client"]` 加载侧栏面板与两张卡片。

### 重启后 30 秒验收清单

```powershell
# 0) 先静态确认加载路径（不需要重启）
node scripts/compose-check.mjs
# 期望 RESULT: 13/13 passed

# 1) 桥起来了（宿主插件已加载）
Invoke-WebRequest http://127.0.0.1:47311/uai/ping -UseBasicParsing | Select-Object -Expand Content
# 期望 {"ok":true,"service":"uai-collab",...}

# 2) 状态可读（核心命令 + 数据面配对环境变量后）
Invoke-WebRequest http://127.0.0.1:47311/uai/status -UseBasicParsing | Select-Object -Expand Content | Select-Object -First 1

# 3) 侧栏出现「UAI 协作」，点开是 中枢/进程/监督 + 席位表 + 两个按钮
# 4) 新会话里让助手调 uai_status / uai_detect，思考链里应是两张卡而不是一行 JSON
```

参考：`@deepseek-ai/dsh-experimental-voice-input-bundle` 的 `dsh.bundle.patch` + `cordis.patch.yml`，
本包按同一形状写（这是 DSH 认 bundle 的唯一入口，缺了就会「装上了但不起作用」）。
