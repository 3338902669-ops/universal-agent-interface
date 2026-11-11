# 统一数据模型 v1（三仓共用）

对应待完善项 **D-02 / D-03 / D-04 / D-05 / C-01**。
本文件是**规范性**的：三仓的机器可读契约必须与本文一致，由 `stage2/check-cross-repo-vocab.py` 强制。

- 适用：`agent-orchestra`（AO，协作层）· `governed-agent-runtime`（GAR，治理层）· `universal-agent-interface`（UAI，互操作层）
- 机器可读副本：`evidence-floor.json`、`evidence-record.schema.json`

---

## 1 状态是两根轴

| 轴 | 值域 | 所有者 | 谁能写 |
|---|---|---|---|
| `execution_state` | `QUEUED` `DISPATCHED` `CLAIMED` `RUNNING` `SUCCEEDED` `FAILED` `TIMED_OUT` `CANCELLED` | UAI | 只有中枢 |
| `governance_state` | `active` `blocked` `closed` | AO | 只有 AO 引擎 |
| `verdict` | `通过` `带注通过` `条件通过` `不通过` `未核` `作废` | UAI 记录 / AO 规则 | 只有核验者，一次一写 |

**桥接规则（唯一）**：

```text
execution_state ∈ {QUEUED..CANCELLED 非终态}      → governance = active
SUCCEEDED + 无裁定                                → governance = active（不是 closed）
SUCCEEDED + {通过, 带注通过, 作废}                → governance = closed
SUCCEEDED + {不通过, 条件通过}                    → governance = active
SUCCEEDED + 未核                                  → governance = blocked
FAILED / TIMED_OUT / CANCELLED + 作废             → governance = closed
FAILED / TIMED_OUT / CANCELLED + 其他             → governance = active
显式阻塞（三轮失败等）                             → governance = blocked（与执行态无关）
```

**禁止**：拿 AO 的 `done` 当执行态；拿 UAI 的 `SUCCEEDED` 当"通过"。

## 2 对外投影（A2A）

| execution_state | A2A TaskState |
|---|---|
| QUEUED / DISPATCHED | `TASK_STATE_SUBMITTED` |
| CLAIMED / RUNNING | `TASK_STATE_WORKING` |
| SUCCEEDED | `TASK_STATE_COMPLETED`（message 必带裁定或「待核验」） |
| FAILED | `TASK_STATE_FAILED` |
| TIMED_OUT | `TASK_STATE_FAILED` + 原因码 `timed_out` |
| CANCELLED | `TASK_STATE_CANCELED` |

**`TASK_STATE_REJECTED` 不映射**：A2A 的 rejected 是"拒绝受理"，与「不通过」无关。

## 3 证据 = 等级 × 载荷

- **等级**（AO）：E1 命令+退出码+修订号；E2 指名同伴且非作者；E3 带注记；E4 写计划。
- **载荷**（UAI）：`uri` / `sha256` / `bytes` / `media_type`。
- 两者都要：只有等级无法证明"是那一份"；只有载荷无法区分"跑过"和"有人说跑过"。

**下限取严**：见 `evidence-floor.json`。`floor(rigor, cls) = rigor.allowed ∩ {强度 ≥ cls.min_grade}`。

**两轴正交**：`rigor` 管证据强度，`cls` 管副作用与审批。只读的 L3 任务仍须 E1。

**封顶规则**：跳过核验不使结论消失，而是**降低证据等级封顶**；想拿高等级必须过核验关。

## 4 审批：scope 就是信封摘要

```text
action_digest = sha256( canonical_json(envelope 去掉 approval 字段) )
scope         = action_digest
```

- 凭据有效 ⟺ `credential.envelope_sha256 == action_digest` 且一次性、未过期
- **不得**用自由文本 scope
- 审批人身份：`approver_authenticity ∈ {asserted, authenticated}`，当前只能是前者

## 5 责任矩阵（单写者）

| 字段 | 唯一写入者 | 其他层 |
|---|---|---|
| `execution_state` / `attempt` / `fence` / `lease_expires_at` | UAI 中枢 | 只读 |
| `governance_state` | AO 引擎 | 只读 |
| `rigor` | AO（任务创建时） | 不可改 |
| `cls` | 任务提交方 | 不可改 |
| `verdict` | 核验者 | 只读；`驳回` 只读别名 |
| `evidence` | 断言者追加 | 不覆盖 |
| `grant` | GAR gate | 只读 |
| `approval` | 审批方签发 | UAI 校验 |

**冲突**：两层写同一字段 → 后写者被拒 + 审计事件。

## 6 四个信任问题必须分开命名

| 问题 | 负责层 | 机制 | 现状 |
|---|---|---|---|
| 接口认证（对面是谁） | UAI | token 哈希 + HMAC 常量时间比较 | 已实现（单域） |
| 操作授权（能不能做） | GAR | grant：单次、过期、绑定单目标 | 已实现 |
| 执行可信（真做了吗） | GAR/UAI | 操作账 intent→confirmed/failed/unknown | 已实现 |
| 结果可信（做得对吗） | AO + UAI | 核验者≠实现者 + 六值裁定 | 已实现 |

**不得互相顶替。**

## 7 一致性强制

三仓 CI 必须跑同一份 `check-cross-repo-vocab.py`。任一侧枚举漂移即红。

---

- 版本：v1，2026-11-12
- 变更：任何枚举变更必须先改本文件与 `evidence-floor.json`，再改实现
