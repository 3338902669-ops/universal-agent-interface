# UAI 分发与接入形式：调研结论与选型（2026-11-17）

> 问题：要把 UAI 做成「装上就能用」的东西——自动检测本机 agent、自动搭好通信与协作底座、
> 有一个启动按钮完成能力检测与分工、按钮后可进入协作模式、并且在模型思考链里实时看到
> agent 调用。技术接入方式（MCP / 插件 / 自动化脚本 / skill）先调研再定。
> 本文是**决策文档**：含证据出处（本机可复查）、选型理由、改造清单与分阶段验收。

## 1 需求拆成四条硬要求

| # | 要求 | 技术含义 |
|---|---|---|
| R1 | 普遍适用（不只这台机器） | 核心不绑 OS、不绑宿主、不绑厂商；Windows 专属只留在适配层 |
| R2 | 装上插件后自动检测 agent + 自动搭底座 | 需要能在宿主里跑代码、读写文件、起停进程的宿主端服务 |
| R3 | 启动按钮 → 能力检测 → 分工（可自定义）→ 进入协作模式 | 需要**常驻 UI 入口**与按钮，且能调用宿主端服务 |
| R4 | 思考链里实时看到 agent 调用 | 需要把调用事件渲染成对话内卡片，而不只是外挂日志页 |

## 2 四条路线各自能做到什么（本机实测证据）

| 路线 | 能做 | 做不到 | 证据 |
|---|---|---|---|
| **Skill（SKILL.md）** | 触发词路由；把安装/使用流程教给模型；跨宿主通用 | **不能画按钮**、不能常驻、不能持有进程 | `~/.dsh/skills/*` 全是文档 + 脚本引用；`skill` 工具只返回文本 |
| **MCP server** | 把 UAI 能力暴露成工具；任何 MCP 宿主（Codex / Claude Code / Cursor）都能调 | **没有任何 UI 概念**；不能加页面/按钮；进不了思考链渲染 | 本机 `dsh-mcp*` 是客户端侧 MCP 桥；工具只会产生文本/结构化结果 |
| **DSH 插件（bundle）** | 宿主端服务（起停进程、读写文件）+ Client UI（左栏页面、按钮、右栏面板）+ **按工具名定制卡片** | 只对 DSH 生效；要按 profile/bundle 打包分发 | `dsh-client-ui-plugin-manager` 就是靠插件占据侧栏「插件」页；`dsh-client-ui-tool` 明写支持业务包注册 `tool.call.toolview` |
| **自动化脚本** | 检测/安装/起停/健康检查 | 没有 UI、没有状态呈现；**本次事故正出在这里**（脚本无停止、叠实例） | `~/.shared/uai-hub/run-uai-hub.ps1` → UAI-12 / UAI-18 |

### 2.1 决定选型的两条关键能力点

**（a）按钮只能是插件。** MCP 协议没有 UI 概念；skill 是文本；个人页（personalized page）是一次性卡片，不是常驻入口。只有 DSH 插件能注册常驻 UI 入口。

**（b）思考链里的实时调用，正好是插件已有的扩展点。** `@deepseek-ai/dsh-client-ui-tool` 把 keyed slot `tool.call.toolview` 暴露给业务包，业务包用它注册自己的工具视图：

```ts
ctx.slots.inject('tool.call.toolview', () =>
  ctx.slots.register({ /* owner: ToolCallOwnerProps = callId / toolName / phase / args / result */ })
)
```

含义：UAI 的每次派发本身就是一次工具调用，**默认就会出现在思考链里**（工具卡片）；再把卡片换成 UAI 自己的视图（派给谁、状态机、耗时、裁定），就得到「思考链里实时看到 agent 调用」，不需要另造面板。

## 3 结论：分层混合，职责不重叠

```text
┌────────────────────────── 任意宿主 ──────────────────────────┐
│ DSH（本机主控）        Codex CLI        Claude Code / Cursor │
│      │                    │                    │            │
│  uai-dsh-bundle        uai-mcp(stdio)      uai-mcp(stdio)    │
│  （插件：按钮/页面/            └────────┬───────────┘         │
│    思考链卡片/自动检测）                 │                     │
│      └──────────────┬───────────────────┘                     │
│                 uai-core（跨平台 Python 守护进程）             │
│  席位注册 · 派发 · 租约围栏 · 三层验收 · 账本 · 图谱回写       │
└──────────────────────────────────────────────────────────────┘
```

| 产物 | 形态 | 唯一职责 | 谁用 |
|---|---|---|---|
| `uai-core` | Python 包 + 可执行 | 协议与运行时不依赖任何宿主；OS 差异关进适配层 | 所有接入方 |
| `uai-mcp` | MCP server（stdio + HTTP） | 让**非 DSH** 宿主把 UAI 当工具用（派发/查状态/看裁定） | Codex、Claude Code、Cursor、任何 MCP 客户端 |
| `uai-dsh-bundle` | DSH bundle（npm 包） | R2/R3/R4：自动检测、一键搭底座、启动按钮、席位卡、思考链视图 | DSH 用户 |
| `uai-collab`（SKILL.md） | 技能 | 发现与引导：何时用、怎么装、装完怎么用；跨宿主说明书 | 所有宿主里的模型 |

**一句话**：MCP 负责「别人能调我」，插件负责「一键跑起来并看得见」，skill 负责「模型知道该用它」，脚本退化为插件的实现细节（不再是人工入口）。

## 4 为什么不选「只用 MCP」或「只用 skill」

- 只用 MCP：按钮、自动检测、分工界面全缺；也进不了 DSH 的思考链渲染。
- 只用 skill：装完仍要用户自己敲命令起服务——本次「叠两套中枢」正是这么来的。
- 只用插件不做 MCP：DSH 之外的席位（Codex 独立跑、未来别的宿主）接不进来，违背 R1。

## 5 R1「普遍适用」改造清单（已审计）

grep 实测（不含文档）的 Windows 强耦合点：

| 位置 | 耦合 | 改造 |
|---|---|---|
| `stage2/hub_singleton.py` | `msvcrt.locking` / ctypes `OpenProcess` | 已有 POSIX 分支（`flock` / `os.kill`）；抽象成 `uai_core.os.lock` 并补 POSIX 测试 |
| `stage2/resident_health.py`、`stage2/drill_resident_plane.py` | PowerShell + `Get-CimInstance` 数进程 | 换 `psutil` 按 cmdline 匹配；服务托管走平台适配（systemd / launchd / 计划任务 / Startup） |
| `~/.shared/uai-hub/run-uai-hub.ps1` + `.vbs` | PowerShell + wscript 隐藏启动 | 监督循环下沉到 `uai-core serve`（Python）；插件负责安装时注册开机自启 |
| `prototype/*.ps1`、`bench/*.ps1` | 仅运维便捷脚本 | 保留，但核心不依赖 |
| 4 个文件里的 `C:\Users\33389\...` 硬编码 | 绝对路径 | 改为 `UAI_HOME` + 默认 `~/.uai` |
| 席位适配器（codex / tianshu / xiaoyu） | 各厂商 CLI 的 argv | 保留「一厂商一适配器」，由检测结果自动生成配置 |

目标：`uai-core` 仅依赖 Python 3.10+ 标准库（+ `psutil`、可选 `nats-py`），在 Linux/macOS 不做任何 Windows 假设。

## 6 R2 自动检测：三层探测表

| 层 | 检测什么 | 方法 | 产出 |
|---|---|---|---|
| L1 存在性 | 装了哪些 agent | 可执行文件 + `--version`（PATH / 常见安装路径 / `which` / `where`）+ GUI 进程名 | 候选清单 |
| L2 可用性 | 能不能真跑一次 | 一次性 `--help` 或最小 dry-run（无副作用、有超时） | 可用/不可用 + 版本 |
| L3 能力 | 擅长什么 | 适配器声明的能力卡（模型、工具、沙箱、能否写文件/跑命令）+ 可选探针任务 | 能力矩阵 |

候选写进 `detect/probes.json`，可扩展：`codex`、`claude`（Claude Code）、`cursor-agent`、`gemini`、`aider`、`ollama`、`rivet`（天枢）、Grok/GUI 类席位。**未识别的实现不假装支持**：标 `unknown` 并让用户手填命令——这就是「可自定义」的入口。

## 7 R3 启动按钮与分工

```text
侧栏「UAI 协作」页
  ├─ [检测并搭建]  第一次点：探测 agent → 生成 seat 配置 → 起 NATS/hub → 注册席位 → 自检 14 项
  ├─ [开始协作]    每次点：能力检测 → 分工草案（可编辑）→ 进入协作模式
  ├─ 席位卡：名称 / 厂商 / 能力标签 / 状态灯 / 最近任务
  └─ 底部：账本尾 20 条（派发、裁定、失败）
```

分工草案不新造算法，复用已有模块：`task_decomposer` 出子任务 → `skill_taxonomy` 归一技能 → `smart_router` 按「能力匹配 + 历史成功率」给建议 → **展示给用户逐条可改** → 确认后落盘 `seats.yml`。

## 8 R4 实时可见性：三层，从内到外

| 层 | 载体 | 看到什么 | 实现 |
|---|---|---|---|
| 1 | 对话内工具卡片（`tool.call.toolview`） | 每次派发：派给谁、状态机 QUEUED→RUNNING→SUCCEEDED、耗时、裁定 | 插件注册自定义视图 |
| 2 | 右侧栏面板 | 全部席位实时流（订阅 hub 事件） | Client 插件 + 宿主服务推送 |
| 3 | 账本（JSONL 哈希链） | 事后可核对的唯一真相 | 已有 |

## 9 分发方式

- 形态：npm 包（bundle），声明 `dsh.profile.bundles` 与 `platform` 支持范围；
- 安装：DSH 插件页支持「本地目录 `file:`/`link:` / git / 注册表」三种来源（`dsh-plugin-manager` 已有 `installBundle` / `inspect`）；
- 上架：需要插件市场索引 URL（`dsh-plugin-market` 的 `indexes`）；
- 兼容：包声明 DSH 版本范围与平台，装不了明确报 `unsupported-platform`，不静默失败。

## 10 分阶段实施与验收

| 阶段 | 内容 | 验收（可复现） |
|---|---|---|
| P0 去机器化 | `uai-core` 抽包：`UAI_HOME`、OS 适配层（锁/进程/服务）、psutil 替代 CIM；现有测试全绿 | 核心测试绿 + 无 PowerShell 路径下健康检查等价可用 |
| P1 MCP 入口 | `uai-mcp`：`uai_dispatch` / `uai_status` / `uai_verdict` / `uai_seats` | Codex 侧经 MCP 派一单并拿到裁定（E1） |
| P2 DSH bundle | 左侧页 + [检测并搭建] + [开始协作] + 席位卡 + 账本尾 | 全新 profile 装包 → 点按钮 → 四席起来 → 自检全绿（E1）；重复点击不叠实例 |
| P3 思考链视图 | 注册 `tool.call.toolview` 的 UAI 视图 | 一次派发 = 对话里一张卡，状态随事件变化（截图/录屏） |
| P4 skill + 上架 | `uai-collab` SKILL.md + 索引条目 | 换一台机器按 skill 装通（Linux/macOS 各一次） |

## 11 风险与「别重复造」

1. **DSH 自带 Agent Teams**（`dsh-agent-team` + `dsh-client-ui-agent-team`）：已有共享任务板、认领/依赖、CAS、UI。**不重复实现**——UAI 的差异化只在「跨厂商、跨进程、可审计」，进程内多 agent 协调直接复用 Agent Teams。
2. **官方可选插件目录（15 个）没有覆盖「跨厂商席位 + 可审计账本」的条目**（forge = 变更请求面板 + worktree，其余为语音/终端/检索等），所以这条能力必须自己做成 bundle。
3. **插件跑在宿主进程内、不受工作区沙箱限制**（`dsh-plugin-manager` 明写），所以自动检测、自动起服务要在 UI 上写清楚，并默认先 dry-run 再落盘。
4. **自动检测不能靠猜**：探测失败标 `unknown` 让用户填，不伪造能力卡——这是本次会话反复踩的坑。

## 12 待你定的事项

1. 包名与作用域：`@uai/core`+`@uai/mcp`+`@uai/dsh-bundle`，或先本地 `file:` 装、确认可用再定名。
2. 第一版支持哪些厂商：建议 codex + claude + rivet(天枢) + DSH 自身，其余按探测结果标 `unknown`。
3. bundle 的 UI 骨架：手工写，还是先用 DSH 的 Creator 预设生成再改（我倾向核心手写、UI 用 Creator 生成）。
