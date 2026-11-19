# 四席接入方式矩阵（2026-11-18 实测）

> 起因：面板显示「4 席运行中」，但接入方式各不相同，能不能干活差别很大。
> 本文只记录**实测过的**事实，不写推测。

| 席位 | 接入方式 | 同步性 | 实测证据 |
|---|---|---|---|
| `codex` | `codex_adapter` → codex CLI 直通（自带 provider 配置） | 同步，~10-50s | 派单回 `UAI-SEAT-OK`，真 usage（input 53124 / output 8） |
| `dsh` | `dsh_adapter` → `dsh --profile headless --permission-mode …` | 同步，~10-25s | 修复凭据后回 `DSH-SEAT-OK`，`[llm-deepseek-api-key] loaded 2 model(s)` |
| `tianshu` | `tianshu_adapter` → `rivet -p … --json --new` | 同步，~10-20s | 能答；**跑命令要仓库白名单**（沙箱拒绝 workspace 外路径，exit 1） |
| `xiaoyu`（小羽 / Grok Bot） | `xiaoyu_adapter` → 写 `muse-bridge/wake-msg.txt` → **GUI 窗口唤醒（UIA 注入 Grok Bot）** → 结果落 `cloud-outbox/comms-tasks/<id>.txt` | **异步**，实测 24s 往返 | 派单 `8d3e5b01…`：t+21s 出现 `comms-tasks/3c19650bbb0b.txt` = `XIAOYU-OK`；hub 侧 `SUCCEEDED`，`exit_code 0`，`trigger: schtasks_ok` |

## 两条容易搞错的结论（我踩过）

1. **小羽不是靠 muse/openclaw CLI 唤起的**。真正的唤醒是**桌面 GUI 窗口**（Grok Bot 应用），
   seat 只负责写 wake-msg + 触发唤醒，然后**等** `comms-tasks` 落文件；拿到才回填 result.json。
   所以它不是「假成功」——`xiaoyu_no_result` 只在超时没拿到结果时才出现。
2. **tianshu 能推理但受沙箱约束**：让它跑 `pytest` 必须先给仓库白名单工作目录，
   否则报 `requires explicit user approval`（实测 exit 1）。

## 对四件套的含义

| 环 | 谁 | 现在能不能跑 |
|---|---|---|
| 实现 | dsh | 能 |
| 审代码 | codex | 能（已实测审出 2 个真缺陷） |
| 实测验 | tianshu | 需先配 work-root 白名单 |
| 回读对账 | xiaoyu 或 dsh | 小羽能回执（异步 24s），dsh 自己能对账 |
