# LIBRARY-SCAN — DSH 应用内 agent 协作控制面板 · 模板侦察

> 扫描时间：2026-11-17 · 只读侦察，未修改任何资源库文件
> 目标物：11 个 agent 席位（名字/厂商/状态/能力标签/最近结果）＋ 实时事件时间线 ＋ 账本尾 ＋ 两个主按钮（检测并搭建 / 开始协作）
> 结论先行：**推荐候选 A**。三个候选的令牌在下文逐值标注出处，无一处为估计值。

## 0 · 路径别名与证据边界

| 别名 | 实际路径 |
|---|---|
| `RWC/` | `C:\Users\33389\.dsh\references\reverse-web-corpus-20261117\\` |
| `UPM/` | `C:\Users\33389\.dsh\skills\ui-ux-pro-max\data\\` |
| 截图证据 | `C:\Users\33389\plain\reverse\motion-library\sites\<site_id>\screens\\`（已确认存在；site-061 下 21 张：cold-00…03 / settled / stage-* / m390 / m768 / reduced-motion / hover / nav-open） |

**本轮实际读取过的文件**（断言均来自这些文件；未运行浏览器、未看截图、未跑 `UPM/scripts/search.py`）：
`RWC/manifest.json`、`RWC/INDEX.md`、`RWC/patterns/{module-stack,monochrome,asymmetric,editorial-type}.md`、`RWC/replication/REPLICATION-METHOD.md`、`RWC/replication/recipes/{site-061,site-067}.md`、`RWC/resources/index.json`、`RWC/templates/site-0{11,21,27,38,52,56,58,61,67}/tokens.json`、`RWC/templates/site-0{38,61,67}/index.html`（另对 100 份 `index.html` 做了全量 `:root` 与结构正则统计）、`UPM/styles.csv`、`UPM/colors.csv`、`UPM/typography.csv`、`UPM/motion.csv`、`UPM/ux-guidelines.csv`、`UPM/ui-reasoning.csv`。

---

## 结论

**推荐候选 A：「RPA / Automation Dashboard」（UPM 语义配方）＋ Data-Dense Dashboard 密度变量 ＋ RWC site-061 骨架。**

一句话理由：库内唯一一条明确把「多席位状态 + 失败可见 + 事件时序」写成硬约束的配方就在这里（`UPM/ui-reasoning.csv` 行 No=188，Confidence 0.93；同源 No=166 状态页 0.94，Reasoning 原文 "operators need instant state recognition and a reliable incident chronology"），而且它自带可直接落地的密度变量（row 36px / gap 8px / padding 12px / small 12px）——这三件事恰好是三个手搓草稿被判丑时最缺的：**层级没有刻度、密度没有节奏、状态没有语义色**。

三个候选的差异一句话：
- **A** 深底运维台：状态色系（绿/琥珀/红）+ 36px 行高 + 11/12/14/16/24/32 阶梯，信息密度最高；
- **B** 深底新闻编辑台（site-061）：**零色相**（accent 就是前景 #efefef），靠 11px、字距 .14em 的大写小标签 + tabular-nums 撑层级，动效全库最静；
- **C** 浅底杂志索引（site-067）：1fr : 1.35fr 左标签右正文的不对称列 + 单一电蓝 #0000ee + 正字距 0.6px，最"印刷"，密度最低。

---

## 候选 A · 深底运维台（recommended）

### 来源

| 部件 | 出处 |
|---|---|
| 语义配方（为什么这么排） | `UPM/ui-reasoning.csv` 行 `UI_Category = RPA / Automation Dashboard`（No=188） |
| 配色令牌 | `UPM/colors.csv` 行 No=188 `RPA / Automation Dashboard` |
| 密度/字号变量 | `UPM/styles.csv` Style No=28 `Data-Dense Dashboard`（Style ID `data-dense-dashboard`） |
| 实时状态动效 | `UPM/styles.csv` Style No=31 `Real-Time Monitoring` |
| 动效时长/缓动 | `UPM/motion.csv` 行 #1 / #7 / #10 / #15 |
| 字体配对 | `UPM/typography.csv` 行 No=9 `Developer Mono`（JetBrains Mono + IBM Plex Sans） |
| CSS 骨架 | `RWC/templates/site-061/index.html`（共用骨架，见 §"三个候选共用同一骨架"）＋ `RWC/templates/site-038/tokens.json` 的五级灰阶作备选底色梯 |

### 为什么适合面板（库内原话，不是我的形容）

- `UPM/ui-reasoning.csv` No=188 · `Reasoning`：**"automation ops need dense state surfaces, failure visibility, and ROI proof"**；`Recommended_Pattern` = "Real-Time / Operations Landing"；`Style_Priority` = "Dark Mode (OLED) + Data-Dense Dashboard"。
- 同行 `Anti_Patterns`：**"Slow dashboards + decorative charts + hidden error states"** —— 正好对应"营销站大标题 / 满屏动效 / 隐藏失败态"三宗罪。
- 同行 `Decision_Rules.must_have`：`bot-status-grid`、`queue-depth`、`process-flow`、`exception-alerts`、`roi-metrics`，`if_dashboard: virtualize-lists` —— 11 席位 = bot-status-grid，事件时间线 = process-flow / exception-alerts，账本尾 = roi-metrics。

### 令牌表

| 角色 | 值 | 出处（文件 · 字段） |
|---|---|---|
| 背景 bg1 | `#020617` | `UPM/colors.csv` No=188 · `Background` |
| 卡片/面板 bg2 | `#0E1223` | 同上 · `Card` |
| 三级面 bg3 | `#1A1E2F` | 同上 · `Muted` |
| 前景 fg1 | `#F8FAFC` | 同上 · `Foreground` |
| 次级 fg2 | `#94A3B8` | 同上 · `Muted Foreground` |
| 三级 fg3 | `color-mix(in srgb, #94A3B8 62%, #0E1223)` ≈ `#5F6B7F` | **派生值**（库内无三级灰）。若不接受派生：用同表 `Border #334155`，但对比度仅 ≈2.4:1，只能给编号/时间戳，不能给正文 |
| 边框 line | `#334155` | 同上 · `Border` |
| accent | `#16A34A` | 同上 · `Accent`（Notes 原文 "Dark terminal + running green + failed red + queued amber"） |
| 状态·运行 | `#22C55E` | `UPM/styles.csv` No=31 · `--live-indicator` |
| 状态·失败 | `#DC2626` | 同上 · `--critical-color`；同值亦见 `colors.csv` No=188 · `Destructive` |
| 状态·排队 | `#F59E0B` | `UPM/styles.csv` No=28 · `Secondary Colors` 原文 "warning (amber #F59E0B)" |
| 状态·完成 | `#2563EB` | `UPM/colors.csv` No=1 · `Primary`（No=188 的 Notes 只写 completed blue 未给值，这是库内可取到的唯一"完成蓝"） |
| 字号阶梯 | 11 / 12 / 14 / 16 / 24 / 32 | 11 = `RWC/templates/site-061/tokens.json · type.small`；12 = `UPM/styles.csv` No=28 · `--font-size-small:12px`；14/16/24/32 = `UPM/ux-guidelines.csv` No=74 原文 "Type scale (12 14 16 18 24 32)"（只取其中 6 档，未新增数值） |
| 行高 | 正文 1.5；紧凑行 1.25 | 1.5 = `UPM/ux-guidelines.csv` No=72 "Use 1.5-1.75 for body text"；1.25 的取舍理由见 C 的"不要照抄"第 1 条 |
| 间距节奏 | gap 8 / card-padding 12 / row 36 / header 56 / sidebar 240 | `UPM/styles.csv` No=28 · `--grid-gap / --card-padding / --table-row-height / --header-height / --sidebar-width` |
| 触控/点击间距 | ≥8px | `UPM/ux-guidelines.csv` No=23 "Minimum 8px gap between touch targets" |
| 点击目标 | ≥24×24 | 同上 No=104（WCAG 2.2 AA） |
| 圆角 | `0`（默认）；需要软化时 `4px` | 0 的库内出处：`UPM/styles.csv` No=70 · `--radius: 0px`（checklist 原文 "0px border-radius on ALL elements"）；硬证据：100 份 `RWC/templates/*/index.html` 中**声明 border-radius 的为 0 份**。4px **无库内出处**，是现有草稿 `assets.css · --r-xs` 的兼容值 |
| 动效·hover | 150–200ms，easing `power1.out` | `UPM/motion.csv` #1（`Duration` / `Easing` 字段） |
| 动效·席位入场（stagger） | 250–350ms，`power1.out` | 同上 #7 `Stagger List/Subtle` |
| 动效·面板/视图切换 | 200–300ms，`power1.inOut` | 同上 #10 |
| 动效·「检测并搭建」骨架 | 1200–1600ms loop，`sine.inOut` | 同上 #15 `Loading / Skeleton/Subtle` |
| 动效·实时指示 | `pulse 2s infinite`；刷新间隔 `5s` | `UPM/styles.csv` No=31 · `--pulse-animation / --update-interval` |
| CSS 缓动曲线（GSAP 名 → CSS 落地） | `cubic-bezier(.22,.61,.36,1)` | `RWC/templates/site-061/index.html · :root --ease`；全库 97/100 同值（我对 100 份 `index.html` 统计 `--ease` 直方图：该值 97 份，其余 3 份各 1） |
| 字体 | 序号/数据用 JetBrains Mono，UI 正文用 IBM Plex Sans | `UPM/typography.csv` No=9 · `Heading Font / Body Font`；同行 `CSS Import` 给了完整 @import URL，可直粘 |

### 不要照抄的部分（A）

1. **`--sidebar-width: 240px` 与 `--header-height: 56px` 是整站级常量**（`styles.csv` No=28），DSH 面板常被塞进更窄容器，240px 侧栏会直接压死 11 席位列表 —— 只借 `--grid-gap / --card-padding / --table-row-height`，侧栏宽按容器实测重算。
2. **Style No=31 的 `animation: pulse 2s infinite` + `position: fixed` 告警 toast + `--toast-duration: 5s`**：面板是常驻界面，脉冲只允许出现在「运行中」的 6px 状态点上（现有草稿 `.dot` 正好 6px），且一次只给 1 个活跃席位，不要全席位一起呼吸；toast 不做。
3. **Style No=28 的 `chart zoom on click` / `Export functionality` / Style No=31 checklist 的 `Alert sounds`**：本面板不做图表、不做导出、不做声音。
4. **typography No=5 Minimal Swiss（Inter + Inter）不能用**：`RWC/replication/REPLICATION-METHOD.md` §6.5 明文"禁用：Inter / Roboto / Space Grotesk 作为主字体（早被用滥，一眼 AI 味）"。同理 No=60 Modern Dark Cinema 也是 Inter+Inter，排除；选 No=9。

---

## 候选 B · 深底新闻编辑台

### 来源

`RWC` 站点 **site-061 · The Pudding**（原型 `monochrome`，全库 8 站之一）。令牌文件 `RWC/templates/site-061/tokens.json`，实测参数 `RWC/replication/recipes/site-061.md`，骨架 `RWC/templates/site-061/index.html`。

### 为什么适合面板

- **全库最静的一站**：`recipes/site-061.md` §5 原文「滚动遥测：13 段分镜下 getAnimations() 计数 = 0 / 0 / …（全 0）；峰值 0；区块层视差 0 个、钉住 0 个」——事件时间线最怕入场动效与"新事件滑入"抢注意力，这站天然没有。
- **单色体系**：`recipes/site-061.md` §4 原文「强调色 无（单色体系）」；`tokens.json · colors.accentSource` = `derived-from-fg`。要"克制的色彩"，这是库里最彻底的一档。
- **原型基线本身就把标题压得很低**：`RWC/replication/REPLICATION-METHOD.md` §3 分原型基线表，`monochrome` 行 h1 = **19px**（对比 `editorial-type` 72px / `pinned-scroll` 72px），是同表里唯一"面板量级"而不是"网站量级"的。
- **层级靠标签而不是靠字号**：`index.html` 里 `.eyebrow{letter-spacing:.14em;text-transform:uppercase}`、`.eyebrow__n{font-variant-numeric:tabular-nums}`、`.card__k{letter-spacing:.14em}` —— 席位的厂商名、能力标签、编号全靠这套 11px 大写微标签，不需要大标题。

### 令牌表

| 角色 | 值 | 出处 |
|---|---|---|
| 背景 bg1 | `#191919` | `RWC/templates/site-061/tokens.json · colors.bg` |
| 面/卡 bg2 | `#292929` | 同上 · `colors.surface1` |
| 三级面 bg3 | `#373737` | 同上 · `colors.surface2` |
| 前景 fg1 | `#efefef` | 同上 · `colors.fg`（`recipes/site-061.md` §4 实测对比度 **15.29:1**） |
| 次级 fg2 | `#959595` | 同上 · `colors.muted` |
| 三级 fg3 | `#4c4c4c`（库内无更浅一档，此项即 line，只能作 disabled） | 同上 · `colors.line` |
| 边框 line | `#4c4c4c` | 同上 · `colors.line` |
| accent | `#efefef`（与 fg 同值 = 无彩强调） | 同上 · `colors.accent` + `accentSource: "derived-from-fg"` |
| 状态色（**外接**） | 绿 `#22C55E` / 琥珀 `#F59E0B` / 红 `#DC2626` | **本站没有状态色**，必须从 `UPM/styles.csv` No=28 `Secondary Colors` 与 No=31 `--live-indicator / --critical-color` 外接，否则 11 席位的状态不可辨 |
| 字号阶梯 | 11 / 14 / 29 / 32 / 48 / 64 | 同上 · `type.small / p / h2 / h3 / h1 / display`；实测字重行高见 `recipes/site-061.md` §3（h3 32px/500/lh 32px/字距 -0.8px；正文 14px/400/lh 18.55px；链接 16px） |
| 行高 / 字距 | `1.4` / `-0.02em` | 同上 · `type.lineHeight` / `type.letterSpacing` |
| 间距节奏 | 区块间距 32px；gut `clamp(18px,4vw,64px)`；卡间距 `clamp(10px,1.4vw,24px)` | 32px = `recipes/site-061.md` §2「区块间距中位 32px」；gut = `index.html · :root --gut`；卡间距 = `index.html · .cards{gap}` |
| 容器 | `maxw 78rem`；实测容器宽 1280px | `index.html · :root --maxw`；`recipes/site-061.md` §2 |
| 圆角 | `0` | 该模板 `index.html` 全文无 border-radius 声明；全库 0/100 |
| 动效·进场 | 500ms（本站实测唯一一条载入动画，linear） | `recipes/site-061.md` §5 动效清单：目标 `strong.svelte-jaewtz`，500ms，linear，载入即播 |
| 动效·micro | 500ms（本站 micro 与 entry 同值） | `tokens.json · motion.micro` = 500；`index.html · --dur-micro:500ms` |
| 缓动 | `cubic-bezier(.22,.61,.36,1)` | `index.html · :root --ease` |
| 阶梯延迟 | `70ms × --i` | `index.html · [data-reveal]{transition-delay:calc(var(--i,0)*70ms)}` |
| 焦点环 | `outline:2px solid var(--accent); outline-offset:3px` | `index.html · :focus-visible` |
| 字体 | 源 "Gooper SemiCondensed"；开源替代 Archivo Narrow / Barlow Condensed / Oswald；系统栈兜底 `'Arial Narrow','Helvetica Neue'` | `recipes/site-061.md` §3 字体表 |

### 不要照抄的部分（B）

1. **视口级媒体块**：`index.html` 里 `.sec--media-full{min-height:88vh}` 与 `.media::after{mix-blend-mode:screen; radial-gradient(...)}`、`.media{linear-gradient(150deg,...)}` —— 面板一个媒体格都不该有，全删。
2. **`--dur-micro:500ms`**：hover/焦点 500ms 在面板里会被读成"卡"。改用 A 的 150–200ms（`UPM/motion.csv` #1），保留本站 `--ease` 与 70ms 阶梯。
3. **`--dur-entry:500ms` 的滚动揭示**：`index.html` 的 `data-reveal` 用 `opacity:0; translateY(22px)` + IntersectionObserver + `setTimeout(fb,1200)` 兜底 —— 面板首帧不能"先隐身再淡入"。删 IO，改挂载后一次性 250–350ms 阶梯淡入。
4. **巨字档 `--fs-display:64px` / `--fs-h1:48px`**：`.d0{font-size:clamp(calc(var(--fs-h1)*.9),8vw,var(--fs-display))}` 是营销站量级（`RWC/manifest.json` 全库 `design.display` 分位 min 32 / median 74 / max 170）。面板标题只到 29–32。
5. **condensed 不能当中文正文**：`recipes/site-061.md` §9 风险原文「替代后字形差异明显，需重新调字距」+ `REPLICATION-METHOD.md` §6.4「中文站不要直接拿拉丁字体当正文」→ 中文走系统黑体，condensed 只给数字/编号。

---

## 候选 C · 浅底杂志索引（最"印刷"、密度最低）

### 来源

`RWC` 站点 **site-067 · 032c**（原型 `asymmetric`）。`RWC/templates/site-067/tokens.json` + `RWC/replication/recipes/site-067.md` + `RWC/templates/site-067/index.html`。

### 为什么适合面板

- **它本身就是"索引页"**：`recipes/site-067.md` §2 实测 22 个区块、区块高 501px、区块内边距 20px，绝大多数区块是"标签—正文"对子；`index.html` 的 `.columns{grid-template-columns:minmax(0,1fr) minmax(0,1.35fr)}` 就是一列放编号/厂商、一列放"最近结果"的现成比例。
- **单一强调色**：`tokens.json · colors.accent` = `#0000ee`（`accentSource:"sampled"`，`recipes` §4 记「饱和 100%」）；`RWC/resources/index.json · accent_colors` 里 `#0000ee` 有 **8 站**在用，是语料库里真实存在的"电蓝"惯例，不是编的。
- **浅色高对比**：`recipes/site-067.md` §4 实测 **18.73:1**（bg #ffffff / fg #121212），比 A、B 更适合长时间读数。

### 令牌表

| 角色 | 值 | 出处 |
|---|---|---|
| 背景 bg1 | `#ffffff` | `RWC/templates/site-067/tokens.json · colors.bg` |
| 面 bg2 | `#f5f5f5` | 同上 · `colors.surface1` |
| 三级面 bg3 | `#ebebeb` | 同上 · `colors.surface2` |
| 前景 fg1 | `#121212` | 同上 · `colors.fg`（对比度 18.73:1，`recipes/site-067.md` §4） |
| 次级 fg2 | `#767676` | 同上 · `colors.muted` |
| 三级 fg3 | `#dbdbdb` | 同上 · `colors.line`（库内无更浅一档，只能作 disabled） |
| 边框 line | `#dbdbdb` | 同上 · `colors.line` |
| accent | `#0000ee` | 同上 · `colors.accent` |
| 字号阶梯 | 12 / 16 / 23 / 30（h1 与 h2 同为 30，靠字重 500 vs 700 分档）/ 74 | 同上 · `type.small/p/h3/h1/h2/display`；字重行高见 `recipes/site-067.md` §3（h2 30px/700/lh 33px；h1:正文 = 1.88） |
| 字距 / 行高 | `0.6px` / `1.2` | 同上 · `type.letterSpacing` / `type.lineHeight` |
| 列比 | `minmax(0,1fr) minmax(0,1.35fr)`，gap `clamp(24px,5vw,90px)` | `RWC/templates/site-067/index.html · .columns` |
| 间距节奏 | 区块内边距中位 20px，区块高 501px | `recipes/site-067.md` §2 |
| 圆角 | `0` | 该模板全文无 border-radius；全库 0/100 |
| 动效·进场 | 900ms（entry）/ 260ms（micro） | `tokens.json · motion.entry / motion.micro`；`index.html · --dur-entry / --dur-micro` |
| 缓动 | `cubic-bezier(.22,.61,.36,1)` | `index.html · :root --ease` |
| 可直接复用的关键帧 | `@keyframes slideIn{0%{transform:translateY(2rem);opacity:.01}100%{transform:translateY(0);opacity:1}}`、`@keyframes fadeIn{0%{opacity:.01}100%{opacity:1}}` | `recipes/site-067.md` §5「可直接复用的关键帧」代码块（同块另有 4 条：translateAnnouncementSlideIn/Out、animateMenuOpen、animateAmbient） |
| 字体 | 源 "NHaas Grotesk"；开源替代 Instrument Sans / Archivo / Manrope（Inter Tight 被配方标为"次选"） | `recipes/site-067.md` §3 字体表 |

### 不要照抄的部分（C）

1. **正文行高 `16px @ 16px = 1.0`**（`recipes/site-067.md` §3 实测）—— 拉丁设计里成立，中文面板里必崩。改 1.5（`UPM/ux-guidelines.csv` No=72「1.5-1.75」）。
2. **12 个 fixed 层 + 预加载遮罩**：`recipes/site-067.md` §1「fixed 层 12 个 / sticky 层 1 个 / 有预加载遮罩」，`tokens.json · source.behavior.preloader = true`。面板是常驻 UI，遮罩全删，fixed 只留 1 层 header。
3. **`--ls:0.6px` 全站施加**：16px 正文上的正字距会削弱中文可读性；只给 12px 编号/厂商名用，正文回 0 或 -0.01em。
4. **`--fs-display:74px` 与 .d0 的 8vw 巨字 + `zoom` 揭示（scale(.96)→1，900ms）**：删。
5. **`gap:clamp(24px,5vw,90px)`**：90px 列间距对"标签—值"对子太空；面板用 16–20px（20px 有该站 §2 内边距实测支撑）。

---

## 三个候选共用同一骨架 —— 这是本轮最重要的结构发现

100 份 `RWC/templates/*/index.html` 的 CSS 是**同一份骨架**，只有 `:root` 与区块序列不同。证据：`site-061` 与 `site-067` 的 class 词汇完全一致（`.wrap / .head / .sec / .d0-.d3 / .eyebrow / .lede / .gallery / .cell / .cards / .card / .columns / .split / .link / .flow / .foot / .skip`），且 100/100 都含 `font-variant-numeric` 与 `position:sticky`。另：RWC 全库**没有** `<table>` / `<dl>` / `<ul>` / `<ol>`（100 份统计全为 0）。

所以"换模板"≠"换样子"。真实差异化只能来自四处，落地方案按这四项逐项不同：
1. **主题明暗**（A/B 深、C 浅）；
2. **字号阶梯**（A 11–32 六档、B 11/14/29/32/48/64、C 12/16/23/30/74）；
3. **字距符号**（B `-0.02em` vs C `+0.6px`）；
4. **结构列比与状态色系**（A 行栅格 + 绿/琥珀/红、B 微标签 + 无彩、C 1:1.35 双列 + 单蓝）。

---

## 营销站元素必删清单（带全库统计，不是感觉）

| 元素 | 全库实测 | 本面板处理 |
|---|---|---|
| 全屏 hero / 88–100vh 媒体块 | `index.html · .sec--media-full{min-height:88vh}` 100/100 含该 class；`canvas-spatial` 原型 17 站以全屏介质为主（`RWC/INDEX.md`） | 删。首屏就是"11 席位 + 时间线" |
| 巨字展示 | `manifest.json · design.display` 分位 min 32 / median 74 / max 170 | 面板标题上限 29–32px |
| 滚动吸附 / 钉住 | `manifest.json · motion.scroll.pinned > 0` 仅 4 站；`pinned-scroll` 原型 4/100 | 删。只有列表自身滚动 |
| 视差 | `motion.scroll.parallaxed > 0` 3/100 | 删 |
| 自定义光标 | `tokens.json · source.behavior.customCursor = true` **24/100**（site-058 Displaay、site-056 Production Type 等） | 删。桌面应用内改光标会劫持系统指针 |
| 预加载遮罩 | `behavior.preloader = true` **55/100**（site-067 即 true） | 删 |
| 混合模式/叠色 | `behavior.blend > 0` **23/100**（site-011 Studio Freight 该字段值 98）＋ `mix-blend-mode:screen` | 删。`color-mix` 只用于派生色阶 |
| 滚动入场 reveal | 100/100 模板含 `[data-reveal]`；入场时长中位 666ms（`REPLICATION-METHOD.md` §3） | 保留"挂载后 250–350ms 阶梯"，删滚动触发 |
| 窗口级平滑滚动 | `index.html · html{scroll-behavior:smooth}` 100/100 | 面板内滚动区可留，窗口级删 |
| 满屏视频 | `evidence.videos` 有视频的站 43/100（`REPLICATION-METHOD.md` §3） | 删 |
| 单页 5+ 套动效语言 | `REPLICATION-METHOD.md` §7 纪律 1「一个页面最多 3 种动效语言」；`recipes/site-067.md` 实测滚动动画峰值 35 | 面板只留 2 套：hover 反馈 + 新事件插入 |

---

## 落地映射（推荐 A 直接落地；括号列给 B / C 替换值）

### 1) 颜色

| DSH token | A（推荐） | B | C |
|---|---|---|---|
| `--bg1` | `#020617` | `#191919` | `#ffffff` |
| `--bg2` | `#0E1223` | `#292929` | `#f5f5f5` |
| `--bg3` | `#1A1E2F` | `#373737` | `#ebebeb` |
| `--fg1` | `#F8FAFC` | `#efefef` | `#121212` |
| `--fg2` | `#94A3B8` | `#959595` | `#767676` |
| `--fg3` | `color-mix(in srgb,#94A3B8 62%,#0E1223)`（派生） | `#4c4c4c` | `#dbdbdb` |
| `--line` | `#334155` | `#4c4c4c` | `#dbdbdb` |
| `--accent` | `#16A34A` | `#efefef`（无彩） | `#0000ee` |
| `--green / --amber / --red`（草稿已有） | `#22C55E / #F59E0B / #DC2626` | 同 A（外接） | 同 A（外接） |
| `--violet`（草稿已有） | 库里无直接对应；若保留紫，用 `UPM/colors.csv` No=18 `Primary #7C3AED` 或 `styles.csv` No=43 `--ai-accent #6366F1` | 同 | 同 |

> 与现有 `uai-ui-drafts/assets.css` 的差异：现在是 `--bg1 #232324 / --accent rgb(86,134,254)`。那个 `#5686fe` 在**两个资源库里都不存在**（`RWC/resources/index.json · accent_colors` 无该值；`UPM/colors.csv` 无此行），是手搓痕迹之一，换掉它。

### 2) 字号

| DSH token | 值 | 落地对象 |
|---|---|---|
| `--fs-micro` | 11px，ls .14em，uppercase | 编号 / 厂商名 / 能力标签（B 的 `.eyebrow`、`.card__k` 实测值） |
| `--fs-small` | 12px（A：`styles.csv #28 --font-size-small`） | 时间戳 / 账本尾 |
| `--fs-body` | 14px / lh 1.5 | 席位名 / 事件正文 |
| `--fs-lead` | 16px / lh 1.5 | 最近结果摘要 |
| `--fs-title` | 24px（面板标题上限，落在 `ux#74` 阶梯内） | 面板头 |
| `--fs-num` | 继承 + `font-variant-numeric: tabular-nums` | 所有数字（100/100 模板都含该类，站内即 `.eyebrow__n`） |

### 3) 间距 / 圆角 / 行高

| DSH token | 值 | 出处 |
|---|---|---|
| `--row-h` | 36px | `styles.csv #28 --table-row-height` |
| `--pad` | 12px | 同上 `--card-padding` |
| `--gap` | 8px | 同上 `--grid-gap`；同值见 `ux#23` |
| `--header-h` | 56px | 同上（容器过窄可降，但这是库值） |
| `radius` 阶梯 | **0 / 0 / 4 / 4** | 0 有出处（`styles.csv #70 --radius:0px`、全库 0/100 声明圆角）；4 是现有 `--r-xs`，**无库内出处**，仅兼容。现有 `--r-sm:8 / --r-md:12 / --r-lg:16` 建议全部下调，否则与 0 圆角的排版语言打架 |

### 4) 动效

| DSH token | 值 | 用途 |
|---|---|---|
| `--dur-hover` | 150–200ms | hover / focus-visible（`motion.csv #1`） |
| `--dur-enter` | 250–350ms | 11 席位首次挂载阶梯（`motion.csv #7`，替换全库 900ms 的营销值） |
| `--dur-toggle` | 200–300ms | 视图切换（`motion.csv #10`） |
| `--dur-skeleton` | 1200–1600ms loop | 「检测并搭建」运行中（`motion.csv #15`） |
| `--ease` | `cubic-bezier(.22,.61,.36,1)` | 97/100 库内同值，直接用 |
| `--stagger` | 70ms × index | `site-061 index.html · transition-delay` |
| `--pulse` | `@keyframes pulse 2s infinite`（仅活跃状态点） | `styles.csv #31 --pulse-animation` |
| `--live-interval` | 5s | 同上 `--update-interval` |

### 5) 结构映射（11 席位 / 时间线 / 账本尾 / 两按钮）

- **席位行**：不要用现有 `.seats` 的 3 列卡片网格（V1 草稿做法；8px gap × 9–10px padding 会把"名字/厂商/状态/标签/最近结果"压成一团）。用 A 的 `--row-h 36px` 单行 + `grid-template-columns: 1.2fr 0.8fr 72px 1.6fr 2fr`（主列 1:1.35 的比例思想取自 site-067 `.columns`，状态列定宽），底线 `1px solid var(--line)`（B 的 `.card{border-top:1px solid var(--line)}` 现成）。
- **时间线**：现有 `.ev` 的 `grid-template-columns:16px 1fr` + `:before` 竖线结构可留；新事件插入用 `slideIn`（C 的 recipes 原文关键帧，2rem→0，300ms，`var(--ease)`）。
- **账本尾**：`--fs-small 12px` + `tabular-nums` + `--fg2`，行距取 `--row-h` 的一半（18px），不加卡片边框。
- **两个主按钮**：保留现有 `.btn / .btn.primary` 语义，只换尺寸：高 30–32px（≥ `ux#104` 的 24px 底线）、`--dur-hover 150ms`、primary 用 `--accent #16A34A`。**不要**用 `UPM/colors.csv` No=18 `Primary #7C3AED` 做 primary —— 那是聊天产品的紫，会把这页读成聊天框。

---

## 没找到合适模板时的退化方案

**先说清事实**：库内确实**没有**"高密度列表 / 数据表格"模板。证据：对 100 份 `RWC/templates/*/index.html` 做全量正则统计，含 `<table>` = 0、`<dl>` = 0、`<ul>` = 0、`<ol>` = 0；而 `UPM/styles.csv` No=28 只给变量（row 36 / gap 8 / padding 12），不给骨架。因此**任何"照模板重建面板"的动作都必须是"骨架 + 密度变量"拼装**，不存在单站直接可用的答案。

退化路径（按优先级，每一档都仍满足"不许从空白手搓"）：

1. **只允许用 RWC、不许用 UPM**：以 `RWC/templates/site-061/index.html` 的 `:root` 作唯一令牌源（不改色相），区域骨架从 `.wrap / .eyebrow / .card / .columns / .flow / .foot` 六件里选 —— `.card{border-top:1px solid var(--line);padding-top:1rem}` 就是现成的列表行原语，11 个席位直接写成 11 个 `.card`，行高 = 1rem padding + 14px 正文，天然 32px 节奏。密度差，但不会丑、也不会 AI 味。
2. **若 DSH 主题必须浅色**：整包换 `RWC/templates/site-067/tokens.json`，但行高从实测 1.0 提到 1.5（`ux#72`），gap 从 `clamp(24px,5vw,90px)` 降到 20px（该站 §2 内边距实测值）。
3. **若 UPM 可用但 colors.csv 行选不到合适的**：用 `RWC/templates/site-038/tokens.json` 的五级灰阶当整条 bg→fg 梯（`bg #121212 / surface1 #232323 / surface2 #313131 / line #464646 / muted #929292`），这是库里唯一一份"五档齐全且间距均匀"的灰阶，比 A 的 `#020617/#0E1223/#1A1E2F` 更中性；状态色仍外接 UPM No=188。
4. **兜底纪律（比选哪个模板更重要）**：任何新面板动工前，先把选定 tokens 抄进 `uai-ui-drafts/tokens.md`（本文件 §"落地映射"即雏形），再写 JSX / CSS Modules；评审时对照各候选的"不要照抄的部分"逐条打勾。这是把"太丑"变成可复核项的唯一切口。

---

## 未核实项（不影响令牌可用性，但别当成已验证）

- 未在浏览器打开任何模板，未看 `sites/<id>/screens/` 截图（路径已确认存在），未做像素级对照。
- 未运行 `UPM/scripts/search.py`（文件存在，10.9KB；本文件所有检索结果均由我直接读 CSV 得到）。
- `UPM/colors.csv` No=188 只提供一档次级灰 `#94A3B8`，三级灰 `--fg3` 必须派生或降级使用 —— 若要"零派生"，这是 A 方案唯一缺口。
- 派生值 `#5F6B7F` 未做对比度实测；三档前景建议实现后用 `ux#36`（正文 ≥4.5:1）逐档复测。
