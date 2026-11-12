# 受控对照实验（E-02）

目的：把「有无 skill 编排」这件事放进受控条件里比较，并且**把通道故障与能力故障分开统计**——
否则"跑不通"会被误算成"能力差"。

---

## 1 变量

| 类别 | 变量 | 取值 |
|---|---|---|
| **自变量** | `skill`（有无编排技能包） | `with_skill` / `without_skill` |
| **控制变量** | 模型 | 同一 model id |
| | 工具集 | 同一 allowlist |
| | 权限 | 同一 permission preset |
| | 工作区快照 | 同一 tar sha256 |
| | 题目 | 同一 `fixtures/tasks.jsonl`（逐字一致） |
| | 判分器 | 派单前冻结，sha256 记录在 run 元数据里 |
| | 超时 | 命令行席 1200s / 运营席 1800s |
| **因变量** | 通过率、通道失败率、能力失败率、平均耗时、判分一致性 | 见 §4 |

## 2 失效判据（关键）

一次运行的结果分三类，**不得合并**：

| 分类 | 判定 | 计入 |
|---|---|---|
| `channel_failure` | 进程起不来、超时、退出码非 0 且无输出、连接失败 | 通道失败率 |
| `capability_failure` | 正常退出但答案不正确 | 能力失败率 |
| `pass` | 正常退出且判分通过 | 通过率 |

**规则**：`channel_failure` 不计入能力评分。若某档通道失败率 > 20%，该档整体作废重跑。

## 3 题目

`fixtures/tasks.jsonl` 六道机械判分题。每题字段：

```json
{"id": "T1", "prompt": "...", "scoring": {"kind": "exact", "expect": "..."}, "timeout_s": 1200}
```

`scoring.kind` 支持：`exact`（去空白后全等）、`regex`（`pattern`）、`numeric`（`expect` 与容差）。
判分器必须是**纯函数**：同输入必得同输出，不读网络、不读时钟、不读随机。

## 4 指标（analyze.py 输出）

```json
{
  "tasks_total": 6,
  "attempted": 6,
  "pass": n, "capability_failure": n, "channel_failure": n,
  "pass_rate": 0.0,            // pass / (attempted - channel_failure)
  "channel_failure_rate": 0.0, // channel_failure / attempted
  "capability_failure_rate": 0.0,
  "mean_duration_s": 0.0,
  "scoring_frozen_sha256": "...",
  "fixture_sha256": "...",
  "verdict": "valid | invalid_channel_noise | no_data"
}
```

`verdict=invalid_channel_noise` 当 `channel_failure_rate > 0.2` —— 该次结果不得用于比较。

## 5 怎么跑

```bash
# 1) 冻结判分器（记录 sha256 到 run 元数据）
python run.py --seat cli-seat --arm with_skill --cmd "<你的执行命令模板>" --out raw/run-001

# 2) 分析
python analyze.py --raw raw/run-001

# 3) 汇总两档
python analyze.py --compare raw/run-with raw/run-without
```

`--cmd` 模板里用 `{prompt}` 与 `{timeout}` 占位。

## 6 没有数据时怎么办

**`analyze.py` 在 `raw/` 为空或不存在时必须以非 0 退出**，并打印 `no_data`。
这是硬要求：不允许"空目录也算 0 失败 → 通过率 100%"。

## 7 结果怎么写

`RESULTS.md` 只写**真实跑过**的数据，并附：
- 运行日期、机器、Python 版本
- 两档各自的 `fixture_sha256` 与 `scoring_frozen_sha256`（必须相同，否则不可比）
- 通道失败率是否越过 20% 阈值

**没跑就写「未运行」。** 编数字是本项目最严重的违规。

---

版本：v1，2026-11-12
