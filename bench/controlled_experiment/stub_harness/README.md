# E-02 的两套装置：为什么都在这个仓库里

`bench/controlled_experiment/` 下有两套**互不替代**的受控实验装置。它们是两个 agent
并行做同一批待办（E-02）时各自产出的，合并时**两边都保留**，因为各自解决不同问题。
不合并成一套的原因写在 §3。

---

## 1 这里（`stub_harness/`）：确定性 stub 装置

| 项 | 内容 |
|---|---|
| 后端 | `stub`（确定性），**不调用任何模型** |
| 命令 | `python run.py --backend stub` 然后 `python analyze.py --raw raw/<run>` |
| 证明什么 | 判分纯函数稳定、通道失败与能力失败分开记账、`analyze.py` 能从 raw 算出五项指标 |
| **不证明什么** | **任何模型能力**。`orchestration_delta=0.4` 是 stub 的**设定行为**，不是模型评测 |
| 已有数据 | `raw/run-20261111T234041Z-d5cb962a/`（12 trial，`api_key_present=false`） |
| 真实模型对照 | **未运行**。需要 `UAI_EXPERIMENT_API_KEY` + `UAI_EXPERIMENT_URL`，缺失时 `--backend http` 会**拒绝运行而不是写假数据** |
| 额外价值 | 有一条 HTTP 后端路径（`--backend http`），能把实验指向任意 OpenAI 兼容端点 |

这套的强项是：**零依赖、零成本、任何环境可复现**，并且对"没 key 就不许编数据"做了硬约束。

## 2 上级目录：真实模型装置

| 项 | 内容 |
|---|---|
| 后端 | 真实 `codex exec`（`deepseek-flash`，provider `deepseek`） |
| 命令 | `python run_pair.py --tasks 6 --out raw` |
| 证明什么 | 真实模型在两档能力上的差异（本次结果：**两档各 6/6，delta=0.0**） |
| 已有数据 | `../raw/{with_skill,without_skill}/`（真实模型调用，含逐题 stdout 可复核判分） |
| 自变量 | 只差 `CODEX_HOME`：with=真实 home（32 skills + 64KB AGENTS.md），without=只含 config/auth/catalog 的干净 home |
| 结果与限制 | 见 [../RESULTS.md](../RESULTS.md)：这是**天花板效应**，只能说"这组题区分不出"，不能说"skill 无用" |

这套的强项是：**真的跑了模型**，并把"两档只差自变量"落到具体机制上。

## 3 为什么不合并成一套

1. **判据格式不兼容且都有理由。** 本目录用 `{condition, score, channel_failure, capability_failure}` 逐 trial 记账；上级目录用 `{arm, outcome, reason}` 三分类。后者直接对应它自己 `analyze.py` 的 `pass_rate = pass/(attempted-channel_failure)` 口径。强行统一会破坏任一侧已验证的 `analyze.py`。
2. **题目不同且各自有针对性。** 本目录的 T04 问「E1 证据记录必须具备哪三个字段」（考协议知识）；上级目录的 T04 问「JSON 键名按序输出」（考机械格式）。前者在 stub 下能稳定区分两档，后者会因空格歧义误判（已修）。
3. **依赖面不同。** 本目录只需 Python；上级目录需要本机 codex 二进制与 `DEEPSEEK_API_KEY`，因此**不能进 CI**，而本目录可以。
4. **合并两套已验证装置的风险高于收益。** 两者都有通过的原始数据；为"好看"把它们揉成一套，会失去"两条独立路径互相印证"的价值。

## 4 什么时候用哪套

- 想在**任何机器/CI** 上验证实验装置本身没坏 → 用本目录（stub）。
- 想回答**真实模型有没有差异** → 用上级目录（`run_pair.py`），并接受它的环境要求。

两套的结论**不得混引**：stub 的 `orchestration_delta=0.4` 不能说成"模型提升 40%"；
真实装置的 `delta=0.0` 也不能说成"装置无效"。

---

本目录内容来自 PR #1（分支 `cloud/todo-20261112-codex`），合并时原样保留以便原始数据可复核。
