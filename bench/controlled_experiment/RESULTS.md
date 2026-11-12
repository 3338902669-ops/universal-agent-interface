# E-02 受控对照实验结果（真实数据，未编造）

运行日期：2026-11-12 · 机器：Windows 11（yffffff）· Python 3.12.10

## 1 运行配置

| 项 | 值 |
|---|---|
| 自变量 | `skill` 环境：`with_skill` / `without_skill` |
| 实现方式 | 两档只差 `CODEX_HOME`：with 用真实 home（32 个 skills + 64,378B AGENTS.md），without 用只含 `config.toml`/`auth.json`/`models-catalog.json` 的 lite home |
| 模型 | `deepseek-flash`（provider `deepseek`）· reasoning effort `high` |
| 工具/权限 | 同一 `--sandbox read-only --ephemeral`，同一 argv，同一工作目录 |
| 运行器 | `run_pair.py`（直接 argv 列表调用，不经 shell，保证两档提示词逐字相同） |
| 判分器 | `run.py` 的 `score()`，冻结 sha256 `001b9cc511abbae72ad3534de0191fe8abeea7300bd1af9f2ae68dfaa9e7b41e` |
| 题目 | `fixtures/tasks.jsonl`，sha256 `92d84c9c32b07afa1ef5e5df78a7df0de7de233ffc488d06a118c1c50a47382c`（6 道机械判分题） |

两档 `fixture_sha256` 与 `scoring_frozen_sha256` **完全相同**，`analyze.py --compare` 判定 `comparable: true`。

## 2 结果

| 指标 | with_skill | without_skill |
|---|---|---|
| attempted | 6 | 6 |
| pass | 6 | 6 |
| capability_failure | 0 | 0 |
| channel_failure | 0 | 0 |
| pass_rate | 1.0 | 1.0 |
| capability_failure_rate | 0.0 | 0.0 |
| channel_failure_rate | 0.0 | 0.0 |
| mean_duration_s | 70.8 | 33.18 |
| verdict | valid | valid |

**`pass_rate_delta = 0.0`。**

## 3 结论（含限制，不夸大）

1. **在这 6 道题上，有无 skill 编排没有可测差异**——两档都满分。这是**天花板效应**：
   题目全是一次算数、格式转换、正则示例，属于模型无需外部技能就能稳定完成的机械任务，
   因此这个实验**不能**用来声称"skill 编排无用"，只能说"这组题区分不出"。
2. **均值耗时差异（70.8s vs 33.18s）不作为结论**。n=6 且每档只跑一次，
   差异由单题离群值驱动（with 档 T06 292.6s、without 档 T02 92.8s；其余题两档都在 21s 上下）。
   要谈耗时必须增加重复次数并看分布，当前数据不足以支撑。
3. **通道失败率为 0**，说明这次运行是同方差比较的有效运行（`verdict=valid`），
   不是被通道噪声污染的无效数据。

## 4 本轮实验顺带修掉的两个真缺陷（E1）

实验不是白跑：它暴露了两个用静态检查发现不了的问题。

**(a) 超时没有约束住墙钟时间。**
首轮 `without_skill` 的 T05 配置 `timeout_s=1200`，实测 `duration_s=1828.894`（超出 628.9s）。
根因：Windows 上 `proc.kill()` 只终止直接子进程，而 codex 派生的孙进程仍持有 stdout/stderr
管道句柄，`communicate()` 要等所有持有者退出才返回。
已改为 `taskkill /F /T` 杀整棵进程树（非 Windows 用 `killpg`），并在超时后限时回收管道。
修复后重跑，全部任务正常返回（见 §2 的耗时）。

**(b) 夹具歧义把格式运气当成能力。**
首轮 T04 两档**都**失败且输出完全一样（`alpha, beta, gamma`）。原因是提示词只说"逗号分隔"，
而判分要求 `alpha,beta,gamma`（无空格）——逗号后加空格同样满足"逗号分隔"的中文表述。
这是判分器与提示词的口径不一致，测的不是能力。已把提示词改为"逗号后不要空格"，
重跑后两档均通过。

## 5 原始数据

- `raw/with_skill/attempts.jsonl`、`raw/with_skill/meta.json`
- `raw/without_skill/attempts.jsonl`、`raw/without_skill/meta.json`

每条记录含 `task_id / arm / exit_code / stdout / stderr / duration_s / outcome / reason`，
可逐条复核判分。复现命令：

```bash
python manifest.py --verify MANIFEST.json     # 先确认题目与判分器未被改动
python run_pair.py --tasks 6 --out raw        # 需要 DEEPSEEK_API_KEY
python analyze.py --compare raw/with_skill raw/without_skill
```

**注意**：`run_pair.py` 里的 `CODEX_HOME` 路径是本机绝对路径，换机器需同步修改
`scratch/ce-home-lite` 的构造方式（只含 config/auth/catalog 的干净 home）。
