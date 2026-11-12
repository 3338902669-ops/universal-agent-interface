# E-02 受控对照实验结果

本文件只写真实跑过的数据。没跑的项写「未运行」。

## 1 确定性 stub 自检（已运行）

这是本包的**harness 自检**，backend=`stub`，不调用任何模型，不证明任何模型能力。它只证明：判分纯函数稳定、通道失败与能力失败分开记账、`analyze.py` 能从 raw 目录算出五指标。

- 运行时间：2026-11-12 07:40:41 Asia/Shanghai（UTC 2026-11-11T23:40:41Z）
- 命令：`python run.py --backend stub`（退出码 0）；随后 `python analyze.py --raw raw/run-20261111T234041Z-d5cb962a`（退出码 0）
- raw 目录：`raw/run-20261111T234041Z-d5cb962a`
- Python：3.13.5
- 当时 `git rev-parse HEAD`：`468c54dcb800e74404b426986a6a439a5cd90476`（本项提交之前的 main；工作树已含本目录文件）
- `tasks_sha256`：`bd15505e68f9bd2af0891e75d983ddcefdcb84f811e054ca4f128e7ade9486f1`
- `scoring_sha256`：`84cbecdd71f0a7d8297b24d5b9bf44a00eb9bd4b32431d41cbd87852e0d8e9a6`
- 密钥：未使用（`api_key_present=false`）

`analyze.py` 输出：

```text
RESULT: M1_channel=0.0833 M2_capability=0.1818 M3_exact=0.8182 M4_mean=0.8182 M5_delta=0.4000
```

| 指标 | 全体（12 trial） | without_orchestration | with_orchestration |
|---|---|---|---|
| M1 channel_failure_rate | 0.0833（1/12） | 0.1667（1/6，T06 通道失败） | 0.0000 |
| M2 capability_failure_rate | 0.1818（2/11） | 0.4000（2/5，T02/T04） | 0.0000 |
| M3 exact_match_rate | 0.8182（9/11） | 0.6000（3/5） | 1.0000（6/6） |
| M4 mean_score | 0.8182 | 0.6000 | 1.0000 |
| M5 orchestration_delta | 0.4000 | — | — |

对照水平的 T06 记为通道失败（`empty_channel`），未计入 M2/M3/M4 分母。T02、T04 在对照水平答错，记为能力失败。处理水平六道全对。这是 stub 的设定行为，不是模型评测。

## 2 真实模型对照

未运行。本环境没有配置 `UAI_EXPERIMENT_API_KEY` / `OPENAI_API_KEY` 与 `UAI_EXPERIMENT_URL`。未编造任何模型分数。

如需复跑模型对照：

```bash
export UAI_EXPERIMENT_API_KEY=...    # 只放环境变量，不要写入仓库
export UAI_EXPERIMENT_URL=...
export UAI_EXPERIMENT_MODEL=...
python run.py --backend http
python analyze.py --raw raw/<new-run>
```

## 3 验收自检（已运行，非指标）

| 检查 | 结果 | 退出码 |
|---|---|---|
| `python analyze.py --raw raw/does-not-exist` | `ERROR: raw path does not exist`；未输出指标 | 1 |
| `python analyze.py --raw raw/_empty`（空目录） | `ERROR: missing trials.jsonl`；未输出指标 | 1 |
| 六道题均含 `scoring` 字段；正确答案 20 次重复得 1、错误输入得 0 | 通过 | 0 |
