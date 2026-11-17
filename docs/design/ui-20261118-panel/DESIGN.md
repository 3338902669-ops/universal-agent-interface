# UAI 看板 · 设计与验收（按《网页制作流程 v1》）

> 产物：`panel.css`（唯一令牌与样式源）· `preview.html`（真数据静态预览）· `verify/contrast.mjs`（对比度实测）
> 消费方：`packages/uai-dsh-bundle`（`scripts/build-css.mjs` 把 panel.css 编译成 `lib/panel-css.js`）

---

## 第 0 步 · 开工判定

| 问题 | 答案 |
|---|---|
| 这是什么活 | 已有 v0 草稿（内联样式写的面板）→ 按十阶段**重制**为看板产品页 |
| 为什么不走诊断分支 | 不是「太丑」问题，是「没有设计契约」：令牌未实测、无概念推导、无动效记录 |
| 命中分支 | 3（已有要改）× 5（新界面全程）——十阶段全跑，阶段 5/6 按低运动预算收缩 |
| 为什么可以收缩 5/6 | 流程最高原则：拓宽不换一端；低预算档的动效只有 hover/焦点反馈，不需要 MH 逐帧设计 |

---

## 阶段 1 · 主题卡（theme card）

| 字段 | 内容 |
|---|---|
| 品牌 | UAI（Universal Agent Interface）· 多席协作框架 |
| 用途一句话 | 让人在一屏内看清「中枢 / 席位 / 监督」三层是否活着，并可按需触发一次能力检测 |
| 受众与情境 | 本机开发者；常驻 DSH 左侧栏，长时间开着，偶尔瞟一眼，偶尔点一下 |
| 情感基调 | 冷静、可读、无装饰（机房状态灯，不是营销页） |
| **一个核心隐喻** | **控制室仪表排**：一排表 + 一排灯 + 一张值班表 |
| 内容支柱（≤3） | ① 三层健康 ② 席位持有者与 pid（可核对）③ 本机 agent 能力矩阵 |
| 主转化动作 | 点「检测并搭建」→ `uai detect` |
| **style basis** | **库驱动，不自造**：`ui-ux-pro-max/data/{app-interface,ui-reasoning,colors,typography}.csv` + `docs/design/ui-20261117/LIBRARY-SCAN.md`（100 站扫描得出 RPA/Automation Dashboard 配方）。每次取值都写明来源，禁用「上个项目的配色」 |

**范围声明**：看板（dashboard）不承担派单/编排操作 —— 那是工具与 hub 的职责；看板只负责「看见」。

---

## 阶段 2 · 设计系统（tokens + 实测）

字体：`system-ui / PingFang SC / Microsoft YaHei`（正文）+ `ui-monospace`（所有数字、pid、标签）。
字号阶：11 / 12 / 13 / 16 / 19（5 档，UI 密排 1.45）。间距 8pt 网格：4/8/12/16/24；行高 36px。
圆角 **0**（仪表排）；线分两级：装饰线 1.80:1、控件边框 3.24:1。

### 实测对比度（`node verify/contrast.mjs`，12/12 达标）

| 配对 | 比值 | 门槛 |
|---|---|---|
| 正文 `fg1` / 面板 `bg2` | **17.77:1** | 4.5 |
| 次要 `fg2` / `bg2` | **7.25:1** | 4.5 |
| 三级 `fg3` / `bg2` | **5.35:1** | 4.5 |
| 正文 `fg1` / 页 `bg1` | **19.28:1** | 4.5 |
| 次要 `fg2` / `bg1` | **7.87:1** | 4.5 |
| 运行绿 / `bg2` | **10.42:1** | 4.5 |
| 警示琥珀 / `bg2` | **9.11:1** | 4.5 |
| 故障红 / `bg2` | **4.95:1** | 4.5 |
| 按钮字 / `accent` | **5.74:1** | 4.5 |
| 控件边框 `line-2` / `bg2` | **3.24:1** | 3.0 |
| 控件边框 `line-2` / `bg1` | **3.52:1** | 3.0 |
| 装饰线 `line` / `bg2` | 1.80:1 | 记录（非控件） |

### 令牌相对上一版的实测修正（不是审美偏好）

| 令牌 | 旧值 | 新值 | 原因 |
|---|---|---|---|
| `fg3` 三级文字 | `#5C6B85` | `#7C8AA6` | 旧值在 bg2 上约 3.6:1，**不够 AA** |
| `green` 运行 | `#22C55E` | `#3DDC84` | 提亮后 10.42:1，状态文字直接可用 |
| `amber` 警示 | `#F59E0B` | `#F5A524` | 同上，状态文字可用 |
| `red` 故障 | `#DC2626` | `#F04438` | 旧值在深底上偏暗，达标后 4.95:1 |
| 控件边框 | 与装饰线同值 `#334155` | 新增 `#556784` | 控件边框必须过 3:1，装饰线不必 |

### 禁用清单（这次不做）

1. 不做**卡片墙**：不做 `border-radius + shadow` 的 KPI 卡片栅格（= 品类默认解）
2. 不做**发光边框 / 玻璃拟态**：看板要低干扰，发光会把注意力从数字上抢走
3. 不做**入场动画**：常驻页面每次刷新都动，等于噪声
4. 不做**滚动叙事 / 视差 / pin**：这不是叙事页，是仪表
5. 不做**图标装饰**：状态用点 + 文字双编码，不用感叹号三角等装饰图标

---

## 阶段 3 · 交互模式与运动预算

**交互：Bento（仪表分区）** —— 三块指标 + 两张表以 1px 分隔线拼成整屏，无滚动依赖。

**运动预算：低档**（每屏主类型 1 = hover/焦点微反馈；辅助 0）。
理由：常驻监控页的第一指标是「扫读速度」，任何入场/滚动动效都会抬高读取成本；
需要动的地方只有两处——状态切换（颜色 160ms）与按钮反馈（背景 + 1px 下沉）。
上限检查：tween 0 / ScrollTrigger 0 / pin 0 / 全屏 filter 0 / canvas 0 —— 远低于性能预算。

---

## 阶段 4 · 概念推导

**车辙（品类默认解 + 相反面）**
- 默认解：深色底 + 圆角卡片墙 + 发光边框 + 大号 KPI + 渐变强调色
- 相反面：**无卡片、无圆角、无发光**，只有分隔线 + 等宽数字 + 状态点

**7 个候选视觉系统（跨 4 个材质族）**

| # | 材质族 | 视觉系统 | 为什么共鸣 |
|---|---|---|---|
| 1 | 石 / 混凝土 | 硬边分区、1px 接缝、零圆角 | 控制室墙面，界面像被切出来的，不像浮起来的 |
| 2 | 金属 | 拉丝灰、细噪点、冷调 | 机架面板的金属感，暗示「这是设备状态」 |
| 3 | 纸 / 印刷 | 值班表：等宽数字、表格对齐、标签体 | 班表是「我要核对」的语义，pid 需要对齐比较 |
| 4 | 工业标牌 | 机架贴纸风：小号大写标签 + 状态点 | 大写微字标签是设备标注语言，几乎是零视觉重量 |
| 5 | 空气 | 极低透明度状态晕 | 只作为状态色的浓度表达，不做背景光斑 |
| 6 | 玻璃 | 层叠背光 | **反面参考**：与「低干扰」冲突，列出来是为了明确不用 |
| 7 | 织物 | 细网格纹理 | **反面参考**：纹理在密排小字下会干扰可读性 |

**主系统：1 + 4（石 + 工业标牌）**
**观点句**：一屏之内，先看数，再看灯，最后才看字。
**猜品类测试**：把截图给同行看，能猜到「这是某个本机 agent 运行时的状态看板」但对配色/排版猜不出是哪家 —— 通过。

---

## 阶段 5 · 动效选材（motion plan）

查序：`web-design-kb/tools/motion-search.mjs -k <词> --has-spec`（库：motion-db 1218 条 / valid 1061）。
每条都点开读了实际 prompt 规格，不是只读标题。

| 区块 | 候选 1 | 候选 2 | 候选 3 | 采用 |
|---|---|---|---|---|
| A 顶栏指标（中枢/席位/监督） | Aceternity **Stateful Button**（轻）点击后切换加载→成功状态 | Aceternity **Multi Step Loader**（轻）按步骤依次切换状态 | Framesbase **Live Language Classes**（轻）实时状态指示语义 | 采用 1 的「状态切换」语义，落到状态点颜色 160ms |
| B 席位行 | Aceternity **Expandable Card**（轻）紧凑→展开 | Framesbase **Church Community**（轻）列表滚动 + 悬浮微动 | Framesbase **Travel Journal**（轻）列表滚动 + 视差 | 采用 2 的「行悬浮微动」，仅背景 160ms；**不用** Expanding（看板不需要详情层） |
| C 按钮「检测并搭建」 | Aceternity **Stateful Button**（轻） | Aceternity **Multi Step Loader**（轻） | Framesbase **Vectrus Energy**（轻）按钮悬浮微交互 | 采用 1 + 3：背景切换 + 1px 下沉 |
| D 能力矩阵标签 | Framesbase **Live Language Classes**（轻）逐项高亮 | Church Community 列表 | Travel Journal 列表 | 采用 1 的「逐项就绪」语义，静态标签 + 就绪色 |

**每区块 ≥3 候选且都有可执行规格**：通过（来源与规格见上表，prompt 文件已读，如
`web-design-kb/data/prompts/motionsites/live-language-classes.md`、`data/prompts/church-community.md`）。

---

## 阶段 6 · 动效设计（收缩说明）

低预算档的动效只有两个过渡，规格直接写在 CSS 里（`--uai-dur: 160ms` / `--uai-ease: cubic-bezier(.22,.61,.36,1)`）：

| 元素 | 属性 | 时长 | 缓动 | 触发 |
|---|---|---|---|---|
| 状态点 / 状态文字 | `background-color` / `color` | 160ms | ease | 数据刷新 |
| 按钮 | `background` / `border-color` / `transform` | 160ms | ease | hover / active |
| 列表行 | `background` | 160ms | ease | hover |

**未进 MH**：原因见阶段 0 收缩说明（时间驱动动效为零，无逐帧设计需求）。若将来加状态脉冲，再进 MH 定稿。

**降级**：`prefers-reduced-motion: reduce` → 全部 `transition-duration: 0ms`（已在 CSS 内实现）。

---

## 阶段 7 · 部署

- 网页侧：`panel.css` → `packages/uai-dsh-bundle/scripts/build-css.mjs` → `lib/panel-css.js`（导出字符串，客户端 `apply()` 注入 `<style>`）；
- 预览侧：`preview.html` 直接引用 `panel.css`（同一份，避免 drift）；
- no-JS baseline：面板是 React 组件，无 JS 不渲染 —— 这是宿主环境决定的（不是营销页），已在验收里写明。

---

## 阶段 8 · 内容与证据

- 文案全部来自真实运行数据：hub pid 58084、4 席 pid、`uai detect` 就绪 5/12（dsh, codex, claude, openclaw, rivet）；
- 零编造：没有「赋能/助力/重新定义」，没有客户名/评价/数据；
- 图片：本页不含图片（纯排版 + 状态编码），素材清单为空是有意为之。

---

## 阶段 9 · 验收（本轮实跑）

| 项 | 方法 | 结果 |
|---|---|---|
| 对比度 | `node verify/contrast.mjs` | **12/12 达标**（含 3.24:1 控件边框） |
| 令牌单一源 | `panel.css` 同时供 preview 与 bundle | 通过（build-css 生成，无二次手写） |
| 390 断点 | `@media (max-width:560px)` 单列 + 行改两行 | 已实现（结构验证见下） |
| reduce | `@media (prefers-reduced-motion: reduce)` 全 0 | 已实现 |
| 键盘焦点 | `:focus-visible` 2px 绿环 + 2px offset | 已实现 |
| 状态双编码 | 点 + 文字（「运行中/未运行」） | 已实现（不只靠颜色） |
| Tell Score | 见 `verify/tell-score.md` | 见该文件 |
| 视觉 loop | 截图两跑 | **未做**：本轮无可用截图工具（Playwright 未安装、ego 未经本会话验证）；按流程规定标注「未做视觉人工复核」 |

---

## 阶段 10 · 交付

| # | 交付物 | 位置 |
|---|---|---|
| 1 | 网页本体 | DSH 侧栏面板（bundle 安装后可见）；静态预览 `preview.html` |
| 2 | 源码 | `panel.css` + `lib/client.js`（用 class 渲染） + `scripts/build-css.mjs` |
| 3 | DESIGN.md | 本文件 |
| 4 | 验收记录 | 本文件阶段 9 + `verify/contrast.mjs` 输出 + `verify/tell-score.md` |
| 5 | 素材清单 | 无图片素材（有意）；字体为系统字 |
| 6 | 客户操作说明 | 见 `packages/uai-dsh-bundle/README.md` |

**没做的（如实）**：视觉 loop 两跑、六门禁中的 console/动效存在性/键盘实测 —— 都依赖真实浏览器，标注为 E4。
