# Stage 2：A2A 边界投影层

在**不改动** `prototype/` 与 `adapters/` 的前提下，把内部账本投影成 A2A 对象、把 A2A 请求转成契约信封。

- 规格：`~/.shared/A2A-SPEC-MAPPING.md`
- 契约：`../protocol/contract-v1.md`
- A2A：`../../a2a-official-spec/a2a.proto`（a2aproject/A2A，sha16 `945DF6E34001B2BF`）

## 文件

| 文件 | 作用 | 自检 |
|---|---|---|
| `projection.py` | 纯函数投影：名片→AgentCard、八态→TaskState、事件→A2A 事件、裁定归一 | `test_projection.py` **10/10** |
| `a2a_server.py` | 只读 HTTP：`/.well-known/agent-card.json`、`/tasks`、`/tasks/<id>` | `test_a2a_server.py` **12/12** |
| `inbound.py` | A2A 请求 → 契约 v1 信封（幂等键派生、附件指纹、凭据拦截） | `test_io.py` 1–18 |
| `event_bridge.py` | 账本增量 → A2A 事件 → 只追加出站信箱 | `test_io.py` 19–27 + `test_bridge_joins.py` **27/27** |

合计 **76 条用例**，全 RC=0。

## 跑一遍

```powershell
$py = 'C:\Users\33389\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
cd C:\Users\33389\workspace\projects\universal-agent-interface\stage2
& $py test_projection.py
& $py test_a2a_server.py
& $py test_io.py
& $py test_bridge_joins.py
```

真账本投影（**跑前先确认目标文件不存在**，信箱是追加模式）：

```powershell
& $py event_bridge.py --base ../prototype --out $env:TEMP\a2a-outbox.jsonl --once
```

起服务：

```powershell
& $py a2a_server.py --base ../prototype --port 43811
```

## 设计要点

### 红线

1. **`SUCCEEDED` ≠ 通过**。投影成 `TASK_STATE_COMPLETED` 时，没裁定就写「待核验」。
2. **`heartbeat` 不投影**（存活信号不是进展）。
3. **`REJECTED` 不映射**（A2A 的"拒绝受理" ≠ 契约的"不通过"），有测试对着官方 proto 守着。
4. **产出路径拒绝「驳回」**，只作只读别名。

### 跨记录联表（真账本逼出来的，缺一条就错）

账本不是"一条记录说清一件事"，出站必须联三张表：

| 需要 | 实际在哪 | 不联表的后果 |
|---|---|---|
| 产物指针 | `TASK_STATE.result_pointer`，**不在** `TASK_EVENT` 上 | 70/70 个 `result_ready` 降级成 `bridge_error` |
| 裁定 | 独立 `VERDICT` 记录，且几乎总写在 `completed` 之后 | 30 个已完成任务的裁定对外全变「待核验」，含 4 条「驳回」、1 条「不通过」 |
| 终态 | `TIMED_OUT` 只在 `TASK_STATE` 上，没有对应事件 | 5 个超时任务对外永久停在 WORKING |

### 幂等（三条独立防线）

1. `_seen` 按 `(task_id, seq)` / `("synth", name)` 去重，**启动时从出站信箱回读** → 重启不重发。
   - 注意 `task` 对象把 id 放 `task.id` 里，没有顶层 `task_id`；`_key()` 必须兼容，否则 task 会重复（真账本上 83 → 166）。
2. `_verdict_emitted` 记住每个任务已投递的裁定值；同值不再 synth。
   - 否则「completed 已 join 到裁定」+「synth 再发一遍」= 每任务两条文案相同的终态。
3. `_final_emitted` 记住已投递终态的任务，`synth_terminal` 用它做**跨批次**守卫（不只看当前批次）。

### 裁定语义

- 同一批次内多次判定：**后写覆盖**，只投一条终态。
- 跨批次重判：补发一条，带 `uai/supersedes`。
- 超时对外原因码固定 canonical `timed_out`；hub 内部原因串（如 `task_timeout`）留在 `metadata["uai/reason"]`。

## 真账本实测（`prototype\data\ledger.jsonl`，2,116 条）

```text
run1: {"emitted": 394, "total": 394}   lines=394
run2: {"emitted": 0,   "total": 0}     lines=394        ← 重启幂等

kinds            {'task': 83, 'status_update': 241, 'artifact_update': 70}
bridge_error     0
tasks_with_final 75                                      ← 70 completed + 5 timeout
message_texts    {'待核验': 40, '通过': 5, '带注通过': 5, '条件通过': 5,
                  '未核': 5, '不通过': 5, '作废': 5, 'timed_out': 5}
```

六个 canonical 裁定各 5 次（= 账本真实条数，无重复），「驳回」零残留。

## 边界（没做的）

- 未接 hub 的出站推送：桥是**旁路跟随账本**，不是 hub 主动推。
- 未接 NATS：服务是 HTTP，没走原实验室的 `iface.<id>.control`。
- 未做 A2A 的 streaming / push notification，只做请求-响应。
- 未做入站请求到 hub 的实际投递（只到信封为止）。

## 证据等级

- 本轮全部数据为本机实跑（E1）。
- 三个实质缺陷（D1/D2/D3）、一处桥自身重复、以及多轮测试缺陷，均由**独立 subagent**（全新上下文）复跑与抽查代码发现，属 **E2（体系内互检）**。
- 修复后的二次、三次复验同样由该 subagent 执行。

## 跨仓统一模型（v0.1）

对应三仓联合审计（`~/.shared/JOINT-ARCHITECTURE-AUDIT.md`）指出的 P0：三个仓库各有一套状态/证据/审批语义。

| 文件 | 作用 | 自检 |
|---|---|---|
| `unified.py` | 统一模型实现：两轴状态、证据等级×载荷、floor 表、审批 scope | `test_unified.py` **33/33** |
| `check-cross-repo-vocab.py` | 跨仓词表一致性检查（读三仓真实文件比对） | **17/17**，且**故意漂移时返回 1** |

### 两条轴，不是一条

```text
execution_state  (QUEUED…CANCELLED)   由 UAI 拥有
governance_state (active/blocked/closed) 由 AO 拥有

SUCCEEDED 不决定治理关闭；治理关闭由裁定决定：
  通过/带注通过/作废 → closed
  不通过            → active（打回重做）
  未核              → blocked（缺出口）
```

这解决了审计里最硬的冲突——Orchestra 的 `done`/`blocked` 与 UAI 的 `SUCCEEDED`/`FAILED` 被当成同一词表。

### 证据 = 等级 × 载荷

- AO 的 E1–E4 是**等级**（跑过没有、谁核的）
- UAI 的四字段是**载荷**（是不是那一份）
- `floor(rigor, cls) = max(rigor_floor, cls_floor)`；`rigor`(L1–L3) 与 `cls`(readonly/reversible/external) **正交**

### 一条命令验三仓

```powershell
cd stage2
python check-cross-repo-vocab.py     # 0 = 一致，1 = 有漂移
```

它会同时读：UAI 的 `contract-v1.md` 与两套 schema、A2A 官方 `a2a.proto`、AO 的 `conformance.json`（41 条）、GAR 的 `KNOWN-FINDINGS.md`。
任一侧改词表而另两侧没跟，这条命令就红。

## 架构待完善项的落地（P0 批次）

对应 `~/.shared/ARCH-TODO-LIST-20261112.md`。

| 项 | 状态 | 实现 | 自检 |
|---|---|---|---|
| **A-02** A2A 入站 → 中枢 | **已闭环** | `inbound_bridge.py`：A2A 请求 → 信封 → `hub.submit` → 受理 → 账本 QUEUED | `test_a02_inbound.py` **8/8** |
| **A-03** 投影接出站 | **已实现** | `a2a_server.py` 新增 SSE `/tasks/<id>/events`，跟随账本增量推送 | `test_a03_stream.py` **6/6** |
| **A-04** streaming | **已实现** | 同上（text/event-stream） | 同上 |
| **A-05** 版本协商 | **已实现（最小）** | `inbound.py` 显式 `contract_version`；不匹配由中枢明确拒绝，不静默降级 | `test_a02_inbound.py` 首条 |
| **A-01 / C-03** 跨域身份 | **接口 + 判定规则** | `identity.py`：LocalToken / OIDC / mTLS 可插拔；**无验证器不得升级 authenticated** | `test_identity.py` **10/10** |
| A-07 / A-08 / C-01 / D-02~05 | **已落地** | `protocol/unified/*` + `protocol/NOT-GUARANTEED.md` | `test_protocol_specs.py` **30/30** |

### 版本协商是怎么回事

实验室原型实现的是契约 **v0.2**，本仓契约已是 **v1**。桥第一次投递被中枢以
`unsupported_contract_version` 拒绝——**这正是该有的行为**：不静默降级，不假装兼容。

现在 `inbound.py` 支持显式声明目标版本（`1.0` / `0.2`），`test_a02_inbound.py` 同时验证：
- 用 `1.0` 投 v0.2 中枢 → 明确拒绝
- 用 `0.2` 投 → 受理成功

### 身份为什么只能到 `asserted`

`identity.py` 里的 OIDC / mTLS 验证器在**参考实现中不做验签**，所以它们只会返回 `asserted` 并附 reason
（`jwt_signature_not_verified_in_reference_impl` / `certificate_chain_not_verified_in_reference_impl`）。

这是有意的：**宁可承认没验证，也不假装验证过**。接真 IdP 后把这两个分支换成真验签，`approver_authenticity`
才允许变 `authenticated`。
