# E-02 受控对照实验包（可复现）

本目录是「有无 skill 编排」这件事的受控实验**工具与流程**，不含编造数据。
方法学依据见 [DESIGN.md](DESIGN.md)：同一模型/工具/权限/工作区/题目/判分器，只改 `skill` 一个自变量；
通道失败与能力失败必须分开统计，通道噪声 > 20% 的那一档整体作废。

---

## 1 目录内容

| 文件 | 作用 |
|---|---|
| `DESIGN.md` | 方法学：变量、失效判据、指标定义、红线（没跑就写「未运行」） |
| `run.py` | 运行器 + 纯函数判分器；每次尝试落一行 JSONL，元数据含 `fixture_sha256` 与 `scoring_frozen_sha256` |
| `analyze.py` | 汇总单档指标 / 对照两档；空数据必须非 0 退出并给 `no_data` |
| `manifest.py` | 冻结清单：题目、判分器、运行器、解释器版本；`--verify` 不一致即拒跑 |
| `test_controlled.py` | 本包自检 22 项（见 §5） |
| `fixtures/tasks.jsonl` | 六道机械判分题（`exact` / `regex` / `numeric`） |
| `MANIFEST.json` | 当前冻结清单（真实生成，不是手写） |

## 2 跑一次实验（三档命令）

```bash
# 0) 冻结清单（先做，后跑）
python manifest.py --out MANIFEST.json

# 1) 两档各跑一次；--cmd 里用 {prompt} 与 {timeout} 占位
python run.py --seat cli-seat --arm with_skill    --cmd "<你的执行命令模板>" --out raw/run-with
python run.py --seat cli-seat --arm without_skill --cmd "<同一命令模板，去掉 skill 环境>" --out raw/run-without

# 2) 单档指标 + 两档对照
python analyze.py --raw raw/run-with
python analyze.py --compare raw/run-with raw/run-without

# 3) 复跑前校验是否还是同一套题与判分器
python manifest.py --verify MANIFEST.json
```

`analyze.py --compare` 在下列任一情况判定**不可比**：
`fixture_sha256` 不同、`scoring_frozen_sha256` 不同、任一档 `verdict=invalid_channel_noise`。

## 3 输出指标

`tasks_total / attempted / pass / capability_failure / channel_failure /
pass_rate / channel_failure_rate / capability_failure_rate / mean_duration_s /
scoring_frozen_sha256 / fixture_sha256 / verdict`。
`pass_rate = pass / (attempted - channel_failure)`；通道失败不计入能力评分。

## 4 当前状态（诚实标注）

| 项 | 状态 |
|---|---|
| 实验工具与判分器 | 已实现，自检 22/22（E1 可复现） |
| 冻结清单 | 已生成 `MANIFEST.json`（E1） |
| **真实两档对照数据** | **未运行**。2026-10-26 曾在本机跑过一次同类对照，但 `raw/` 原始目录未在 11-10 清理中留存，也没有随结果提交的 sha256 清单；**无原始数据即不引用其结论**（按 DESIGN.md §7 红线） |
| 结论 | 本包只交付「可复现的流程与方法学」；`with_skill` 与 `without_skill` 的能力差异**尚无可引用数字**，任何引用须等一次带 `MANIFEST.json` 的完整两档运行 |

这一点是刻意的：把流程公开并允许第三方用同样命令复跑，比声称一个无法复算的差值更有价值。

## 5 自检

```bash
python test_controlled.py   # RESULT: 22/22 passed
```

覆盖：判分器纯函数性、三种 scoring kind、六道题结构、空 raw 非 0 退出、三类结果分开计数、
通过率分母扣除通道失败、通道噪声 > 20% 判 `invalid_channel_noise`、fixture 不同判不可比、
`--compare` 遇空数据非 0 退出。

## 6 已知限制

- 判分器只能判**机械可判**的题（全等/正则/数值）。主观题不在本包范围。
- `run.py` 只负责调度与落盘，**不提供模型**；两档的模型/工具/权限由调用方保证一致，元数据只记录不校验。
- 答案泄漏：`fixtures/tasks.jsonl` 里带 `scoring.expect`，若把该文件放进被测工作区，被测方可能读到答案。
  运行时**不要**把 `fixtures/` 放进被测目录（DESIGN.md 已记录该限制）。

版本：v1，2026-11-12。
