# UAI 面板设计令牌（2026-11-17，源自设计资源库，非手搓）

> 依据：`LIBRARY-SCAN.md`（侦察报告）。来源 = `UPM/ui-reasoning.csv` No=188 `RPA / Automation Dashboard`
> + `UPM/styles.csv` No=28 `Data-Dense Dashboard` + `RWC/templates/site-061` 骨架。
> **上一版被判丑的两个根因**：`--accent #5686fe` 与 radius 8/12/16 在两个资源库里都不存在，是手搓痕迹。

| 令牌 | 值 | 出处 |
|---|---|---|
| `--bg1` | `#020617` | UPM colors No=188 |
| `--bg2` | `#0E1223` | 同上 |
| `--bg3` | `#1A1E2F` | 同上 |
| `--fg1` | `#F8FAFC` | 同上 |
| `--fg2` | `#94A3B8` | 同上 |
| `--line` | `#334155` | 同上 |
| `--accent` | `#16A34A` | 同上（primary 用绿，不用聊天产品的紫） |
| `--green / --amber / --red` | `#22C55E / #F59E0B / #DC2626` | 状态语义色 |
| `--fs-micro` | 11px，`letter-spacing:.14em`，大写 | styles #28 --font-size-small 上一档 |
| `--fs-small` | 12px | styles #28 |
| `--fs-body` | 14px / lh 1.5 | ux#72 |
| `--fs-lead` | 16px | 阶梯 |
| `--fs-title` | 24px | ux#74 |
| `--row-h` | 36px | styles #28 --table-row-height |
| `--pad` | 12px | styles #28 --card-padding |
| `--gap` | 8px | styles #28 --grid-gap / ux#23 |
| `--header-h` | 56px | styles #28 |
| radius | **0 / 0 / 4** | styles #70 `--radius:0`；全库 0/100 声明圆角 |
| `--ease` | `cubic-bezier(.22,.61,.36,1)` | 97/100 库内同值 |
| `--dur-hover` | 150ms | motion #1 |
| `--dur-enter` | 280ms，`--stagger 70ms × index` | motion #7 / site-061 |
| 字体 | `IBM Plex Sans` + `JetBrains Mono`（数字 `tabular-nums`） | typography No=9 `Developer Mono` |

## 结构约束（不是审美偏好，是库内硬约束）

- 席位**不要**用卡片网格：用单行 `--row-h 36px` + `grid-template-columns:1.2fr .8fr 72px 1.6fr 2fr`，底边 `1px solid var(--line)`；
- 账本尾 12px + `tabular-nums`，行高 18px（`--row-h` 的一半），不加卡片边框；
- 时间线保留 `14px 1fr` + 竖线；新事件 `slideIn` 300ms；
- 按钮 30–32px 高，primary 用 `--accent`，hover 150ms；
- **必删的营销元素**：全屏 hero、滚动吸附、视差、自定义光标、900ms 入场、装饰性图表。
