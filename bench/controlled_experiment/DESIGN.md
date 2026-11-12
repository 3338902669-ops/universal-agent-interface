# 受控对照实验设计（E-02）

本目录是 `bench/README.md` §3「五席能力测试电池」的**可复现包**：冻结判分、同题逐字一致、仅按条件替换编排、隔离会话、通道失败与能力失败分计。

当前程序仅是实验工具原型，**不能测量编排的因果效果**：处理组直接收到包含题目答案的协议摘录，没有调用编排器；两组所获信息不同。stub 结果是预设行为，不是模型能力或机制收益。真实模型实验尚需重新设计对照、冻结输入并独立验证。

---

## 1 自变量（independent variable）

当前改变的是 **是否提供含答案的附加材料**，不是经过隔离的编排机制：

| 水平 | 值 | 含义 |
|---|---|---|
| 对照 | `without_orchestration` | 席位只拿到题目文本，没有 skill / 参考手册 / 编排器 |
| 处理 | `with_orchestration`（历史字段名） | 直接向提示词加入协议摘录；未调用编排器或检索器 |

禁止同时改变模型、工具、权限或工作区。禁止在对照水平偷偷塞进 skill pack。

## 2 因变量与五项指标

判分器是纯函数：相同 `output` + 相同 `scoring` 必得相同 `score ∈ {0, 1}`（通道失败不打分，`score` 为 `null`）。

`analyze.py` 从一次 `raw/<run>/` 读出下列 **5 项指标**：

| 代号 | 名称 | 定义 |
|---|---|---|
| M1 | `channel_failure_rate` | 通道失败次数 / 全部 trial 数 |
| M2 | `capability_failure_rate` | 在非通道失败的 trial 中，`score == 0` 的比例 |
| M3 | `exact_match_rate` | 在非通道失败的 trial 中，`score == 1` 的比例 |
| M4 | `mean_score` | 在非通道失败的 trial 中，`score` 的算术平均 |
| M5 | `orchestration_delta`（历史名称） | 两组各自准确率的描述性差值；不能证明编排效果。另报告同题两组均有效的配对差值与覆盖率。 |

若某一水平的全部 trial 都是通道失败，则该水平的 M2/M3/M4 为 `null`，M5 亦为 `null`（不得把「通道没通」写成「能力为 0」）。指标按「全体」与「分水平」同时给出。

## 3 控制变量（controls）

以下在两个水平之间必须相同；跑次元数据会记录它们的快照，分析前核对：

| 控制变量 | 如何冻结 |
|---|---|
| 模型（model） | 同一 backend、同一模型名；stub 自检时 backend 固定为 `stub` |
| 工具（tools） | 当前 HTTP 路径未调用工具或检索；两组材料不同，无法把差异归因于编排 |
| 权限（permissions） | 同一工作区权限；不得在处理水平开放对照水平没有的网络或密钥 |
| 工作区快照（workspace snapshot） | 同一 git revision、同一 `fixtures/tasks.jsonl` 字节哈希、同一 `scoring.py` 字节哈希；判分脚本在派单前冻结 |
| 题目文本 | 同题逐字一致，禁止按席改写题干 |
| 会话隔离 | 每个 trial 独立会话，不把上一题的输出带入下一题 |

超时：默认 120 秒/题。超时记为通道失败，不记为能力失败。

## 4 失效判据（failure criteria）

必须把两类失败分开，不得混记：

**通道失败（channel failure）** — 题目没有到达可判分的输出。包括：

- 传输错误、HTTP 非 2xx、鉴权失败（缺密钥或 401/403）
- 超时、进程崩溃、后端无法启动
- 空响应且伴随明确的通道错误码

通道失败 **不进入** M2/M3/M4 的分母。

**能力失败（capability failure）** — 通道已返回一份输出，但纯函数判分 `score == 0`（答错、答偏、格式不对）。

**成功** — 通道已返回输出，且 `score == 1`。

## 5 题目与判分

- 六道题，见 `fixtures/tasks.jsonl`。每题含 `prompt`、唯一 `answer`、`scoring` 字段。
- 判分器实现于 `scoring.py`，全部为纯函数。
- 派单前计算并写入 `raw/<run>/freeze.json`：`tasks_sha256`、`scoring_sha256`、`git_sha`、`python`、`backend`。跑完后若 freeze 与当前文件不一致，`analyze.py` 必须报错退出。

## 6 如何跑

```bash
cd bench/controlled_experiment

# 确定性 stub 自检（无网络、无密钥；用于验证本包本身）
python run.py --backend stub

# 分析一次已存在的 raw 目录
python analyze.py --raw raw/<run-id>

# 对不存在或空的 raw 目录必须非 0 退出，且不得编造指标
python analyze.py --raw raw/does-not-exist ; echo exit:$?

# 可选：真实模型（密钥只从环境变量读；缺了就拒绝进入 live，不写假结果）
#   UAI_EXPERIMENT_BACKEND=http
#   UAI_EXPERIMENT_URL=...
#   UAI_EXPERIMENT_API_KEY  （或 OPENAI_API_KEY）
# 不得把密钥写进仓库、命令行参数或 raw 文件。
```

`RESULTS.md` 只记录真实跑过的数据。没跑过的模型评估写「未运行」。
