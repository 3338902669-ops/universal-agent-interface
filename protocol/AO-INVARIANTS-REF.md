# 与 Agent Orchestra 的引用关系（B-02）

对应待完善项 **B-02**：UAI 的契约正文里重复实现了 AO 的四条不变量。本文件把它们改成**引用**，并给出逐条映射。

原则：**下层已实现且有 pinning test 的，上层不再重写，只引用并声明依赖。**

---

## 1 四条被重复实现的不变量

| UAI 原本自己写了一遍 | AO 的要求 | AO 状态 | AO 的强制点 | 处置 |
|---|---|---|---|---|
| 同一 `resource_key` 同一时间只能有一个活动任务（契约 §1） | **R1** No two live tasks may hold an overlapping resource | ENFORCED | `scripts/check.py` | 改为引用 R1 |
| 核验者必须未参与该次执行（契约 §10） | **V1** The verifier MUST NOT be the implementer at L2 and L3 | ENFORCED | 同上 | 改为引用 V1 |
| 失败的核验阻断下游（契约 §10 + `SUCCEEDED` 按未核） | **V4** A failed verification MUST block done and dispatch to evidence | ENFORCED | 同上 | 改为引用 V4 |
| `cls=external` 必须带审批凭据（契约 §7） | **A1** An external action at its step MUST NOT be dispatched before approval | ENFORCED | 同上 | 改为引用 A1 |

四条都在 AO 的 `conformance.json` 中标记 **ENFORCED** 且有 `pinnedBy` 测试。

## 2 UAI 独有的（不引用，自己实现）

| UAI 条款 | 为什么不能由 AO 提供 |
|---|---|
| 任务信封 11 必填 + 262,144 字节上限 | AO 没有传输层对象 |
| 租约 / attempt / fence 四元组 | AO 只有资源锁，没有执行租约与围栏代次 |
| 事件 `seq` 单调 + `(task_id, seq)` 去重 + 发件箱补投 | AO 没有事件流 |
| 产物指针四字段 + 中枢回读 | AO 的证据是"等级"，没有内容哈希 |
| 六值裁定 canonical 与只读别名 | AO 用 PASS/FAIL 措辞，词表在 UAI 侧 |
| A2A 边界投影 | AO 不涉及外部协议 |

## 3 三条 AO 有、UAI 缺的（已在本仓补齐）

| AO 要求 | UAI 原缺口 | 现在 |
|---|---|---|
| **C1** 三轮失败阻塞 | 无 blocked 态 | `unified.py: failed_verifications>=3 → blocked`（B-01） |
| **C2** 阻塞任务拒绝认领/完成/审批 | 同上 | `reassign.py` 拒绝改派 blocked 任务（A-11） |
| **E1–E4 证据等级** | 只有载荷没有等级 | `protocol/unified/evidence-record.schema.json`（A-07） |

## 4 依赖声明（写进契约时的措辞）

> 本契约在**资源串行（R1）**、**核验者独立性（V1）**、**失败阻断（V4）**、**外部动作审批（A1）**
> 四项上依赖 Agent Orchestra 的实现，不重复定义其判定逻辑；本契约只规定这四项在**传输层与互操作层**
> 的载体（`resource_key`、核验者字段、`verdict`、审批凭据绑定）。
> 冲突时：**判定语义以 AO 为准，载体格式以本契约为准。**

## 5 强制方式

`stage2/test_b02_ao_refs.py` 检查：
1. 本文件列出的四个 AO 要求 ID 在 AO 的 `conformance.json` 中存在且为 ENFORCED；
2. UAI 侧存在对应的载体字段；
3. 三条"AO 有 UAI 缺"的项在 `unified.py` / schema 中确有实现。

兄弟仓未克隆时该测试 SKIP 而不失败。

## 6 V1 在 UAI 侧的强制点（C-05 补齐）

「改成引用」不等于「没人管」。引用之后 UAI 仍需在自己的载体上**拒绝**违反 V1 的记录，
这一层由 `stage2/unified.py: validate_independent_verification()` 承担（测试 `test_p1_batch.py`）：

| 规则 | 拒绝码 |
|---|---|
| `通过 / 带注通过 / 条件通过` 必须带 `basis.independent_verification` | `verdict_requires_independent_verification` |
| `self_check: true` 不是独立核验 | `self_check_is_not_independent_verification` |
| 核验者不得等于执行者（V1 本体） | `verifier_must_not_be_implementer` |
| 核验必须写 `checked_at` | `independent_verification_missing_checked_at` |
| `未核` 必须写负责人 + 最小动作 + 绝对时限 | `unverified_missing_completion_plan` |

`不通过 / 作废` 允许执行者自判——失败判定不需要独立性，只有「通过类」声明需要。

## 7 C-04 在 UAI 侧的处理（证据只认账本）

F-GAR-01 记的是「账本与证据队列可能不一致」。UAI 的做法是把账本当**唯一源**：
证据记录必须带 `ledger = {seq, record_sha256}`，`bind_evidence_to_ledger()` 校验行号存在且摘要
与账本当前内容一致；`check_evidence_store()` 一次扫全库返回冲突清单，用于发现双写漂移。
拒绝码：`evidence_missing_ledger_ref`、`evidence_ledger_ref_dangling`、
`evidence_ledger_digest_mismatch`。

---

版本：v1，2026-11-12
