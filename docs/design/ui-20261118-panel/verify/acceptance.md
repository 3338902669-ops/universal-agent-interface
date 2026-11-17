# 验收记录 · UAI 看板（2026-11-18）

工具：ego-browser（AGENTS 规定的默认浏览器）+ `ai-design-tells` + `verify/contrast.mjs`。
脚本：`C:/Users/33389/ego-panel/round-01…05.mjs`（每轮一个文件，可重放）。

## 1. 六门禁

| # | 门禁 | 方法 | 结果 | 证据 |
|---|---|---|---|---|
| 1 | 桌面全滚 1440 | CDP 设 1440×700 → 截图全文 | **PASS**：`docOverflow = 0`（`scrollWidth - clientWidth`），四列行高 36px，`btnRadius 0px` | `verify/shot-1440-final.png` |
| 2 | 移动 390 | CDP 设 390×844 → 截图 + 量面板容器 | **PASS**：`matchMedia(max-width:560px)=true`，行高 55.24px（两行堆叠），面板宽 390、`scrollWidth-clientWidth = 0` | `verify/shot-390-v3.png` |
| 3 | reduce | CDP `Emulation.setEmulatedMedia(prefers-reduced-motion:reduce)` | **PASS**：`.uai-row` / `.uai-btn` 的 `transition-duration` 归 0，4 行 + 5 tag + 3 指标仍在 | `verify/shot-390-reduce.png` |
| 4 | console 0 error | 静态页无脚本；面板本体在 DSH 内由 React 渲染 | **未验证（E4）**：需装 bundle 重启后在 DSH 内看 console | — |
| 5 | 动效是真的 | 本页运动预算=低档，只有 hover/transition | **未验证（E4）**：需鼠标 hover 前后帧差，本轮未做 | — |
| 6 | 键盘 | 实测 `.focus()` 得到 UA 默认 outline（`:focus-visible` 需真键盘焦点） | **部分**：`:focus-visible` 规则已写（2px `--uai-green` + 2px offset），Tab 实测被 PowerShell 截断，未取到结论 | — |

## 2. 视觉 loop（四跑，每跑写三个最差 → 修 → 重看）

### 跑 1（`shot-1440.png`）
1. 席位行名字与状态之间 **1000px 空白**，眼睛要横跨整屏，行内没形成组
2. 主按钮用 `--uai-accent` 实心绿，**和「中枢运行中」的状态绿同源**，CTA 抢了主信息
3. 能力矩阵块与列表同字号同底色，读起来像列表的延续，**没有层级**

→ 修：行改四列（名/状态/锁源/pid）；CTA 改中性抬升（bg3 + line-2），绿只留给状态；能力矩阵换 bg3 底 + 顶部标题分割线。

### 跑 2（`shot-1440-v2.png`）
1. `os_lock_held` 与 `pid 53416` 粘成一句，**两个 mono 串之间没有分隔**
2. 第四列 944px，内容靠左、右侧仍空 900px
3. 移动端 pid 被挤掉（三行堆叠时 `.uai-row__meta` 与 `.uai-row__src` 抢同一行）

→ 修：顺序改为 名 → 状态 → 锁源 → pid，pid 单独 `text-align:right` 贴右端；移动端改三行（名+状态 / 锁源 / pid）。

### 跑 3（`shot-1440-v3.png`）
1. **出现横向溢出**：pid 被切在右边缘、底部出现横向滚动条

→ 定位：不是面板本身，是**预览壳** `.frame { width:1440px }` 在带垂直滚动条的视口里溢出 15px；改 `min(1440px,100%)`。

### 跑 4（`shot-1440-final.png`）
1. `docOverflow = 0`，pid 完整贴右端，行读作「席位｜状态｜锁来源｜……｜pid」
2. 剩余小问题（记录，不修）：700px 视口出现垂直滚动条（内容高于视口，正常）；能力矩阵只有 5 个 tag，右侧留白偏多

**结论**：视觉 loop 四跑完成，三个最差逐轮收敛。**未做的**：hover 帧差、DSH 内真实渲染的目视复核。

## 3. Tell Score

| 被测 | 分数 | 等级 |
|---|---|---|
| `preview.html`（外链 CSS，检测器读不到样式） | 13.5 | B |
| `verify/preview-inlined.html`（CSS 内联＝线上等价） | **3.8** | **A** |

判定为「保留」的那条 H2 理由见 `tell-score.md`（能力矩阵列 agent 名是数据，不是修辞）。

## 4. 四测

| 测 | 做法 | 结果 |
|---|---|---|
| Swap（换字测试） | 把标题换成「服务器监控」 | 仍成立（看板语义不依赖品牌名） |
| Squint（眯眼） | 缩到 25% 看层级 | 通过：三块指标 → 列表 → 标签，三档灰度分明 |
| Signature（签名） | 遮住标题能否认出 | 通过：0 圆角 + 1px 接缝 + 等宽数字是本页签名 |
| Token（令牌） | 游离色值检查 | 通过：CSS 里除 `#232841`（hover 抬升）外全部走 `--uai-*` 变量 |

## 5. 性能

无 JS、无图片、单 CSS（7.5KB），运动预算低档：tween 0 / ScrollTrigger 0 / pin 0 / canvas 0。
面板本体只在 DSH 侧栏内渲染一次，无首屏图片、无 LCP 竞争。
