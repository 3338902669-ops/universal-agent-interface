# EventBridge v2 重写规格：不变量先行（2026-11-13）

状态：**设计稿 v2（已并入规格核验修订 N1–N3 与 §4 四点修正），实现已授权**。
核验：`RE-spec-reviewer-evbridge-v2-20261113.md` 判定 SPEC_PASS_WITH_NOTES；
本 v2 即修订稿，核验条件（N1/N2/N3 + §4 四点）已全部并入，无未决项。
背景：三轮独立复核（RE-tianshu-event-bridge-verify + RE-agent15 r1/r2/r3）证明现有桥的
缺陷是结构性的——两个互相冲突的生命周期路径（drain 的等待守卫 vs flush 的绕过路由），
加上从 outbox 回读重建的跨崩溃状态，无法靠补丁收敛。

本规格先钉死不变量与状态模型，实现只是它们的机器执行。

---

## 1 目标与非目标

**目标**：把只追加账本（ledger.jsonl）的增量，按账本顺序投影成 A2A 事件流，
写入只追加出站信箱（outbox），满足 §3 全部不变量——包括跨批次、跨重启、崩溃窗口。

**非目标**：
* 不改账本格式、不回写账本（桥永远只读账本）；
* 不实现传输（SSE/webhook 另立层）；
* 不承诺 exactly-once 下游（出站信箱是 at-least-once，消费方按 §5 幂等）。

---

## 2 核心设计决定：单一持久化状态文件，取代"内存态 + outbox 回读"

三轮复核的结构性根因是**桥的状态散在两处**：内存字典（重启即丢）+ outbox 回读
（只有事件形状，没有决策依据）。v2 引入第三个文件：

```
data/
  ledger.jsonl          # 输入（只读）
  outbox.jsonl          # 输出（只追加）
  bridge-state.json     # 桥的持久化状态（原子重写：tmp + os.replace）
```

`bridge-state.json` 内容：

```json
{
  "schema": "uai.bridge.state/1",
  "ledger_committed_offset": 12345,
  "tasks": {
    "<task_id>": {
      "waiting_result": true,          // 有未解决的 result_ready
      "result_pointer": {...}|null,    // 最新指针（可来自 TASK_STATE 或事件自带）
      "states_seen": ["QUEUED","RUNNING"],
      "last_state": "SUCCEEDED",
      "verdicts_emitted": ["通过","不通过"],   // 已对外发布的裁定序列
      "final_emitted": true,
      "final_source": "event"|"synth"|null,    // final 来自哪条路径
      "last_seq_emitted": 2
    }
  },
  "pending_replay": []   // ≤ 少量；见 §4。正常应为空
}
```

**规则 S1**：桥的全部决策只读 `bridge-state.json` + 账本重放，不依赖进程内存。
`_load_emitted` 式的 outbox 回读**只用于校验**（state 文件与 outbox 不一致时 fail-closed），
不用于重建决策状态。

**规则 S2**：state 文件在每次 `write()` 成功后原子更新。
崩溃窗口 ≤ "一次 write + 一次 state 更新"；§6 定义该窗口的恢复语义。

---

## 3 不变量（每条都可机器检验）

### INV-1 账本原序
同一任务的对外事件顺序 == 该任务账本记录的顺序。
**禁止任何按记录类型（events→verdicts→states）的分组重排**——R3 反例 ② 的根因。
检验：对任意任务，取 outbox 中该任务的 `uai/seq`（或 synth 标记对应的账本偏移）序列，
必须与账本中对应记录的单调顺序一致。

### INV-2 无静默丢失
账本中每条非 heartbeat 的 TASK_EVENT 要么产生对外事件，要么产生 bridge_error 审计，
要么**显式挂起且挂起状态可从 state 文件读出**（`waiting_result=true`）。
不允许"留在内存 pending、状态文件里看不到"——R3 反例 ① 的根因。

### INV-3 result_ready 挂起语义（唯一等待）
`result_ready` 且无指针 → 该任务进入 `waiting_result=true`，**后续该任务的所有记录
仍按 INV-1 处理**（吸收状态、记录裁定），只是暂不对外发布事件。
这取代旧设计的"同任务后续事件跟随挂起"——挂起的是**发布**，不是**处理**。
指针到达（TASK_STATE.result_pointer 或事件自带 result_ref）→ 解除挂起，
按账本原序补发积压事件（含 artifact_update）。

### INV-4 final 单调且至多一源
对外 final 事件的序列 = 账本真实时点序列，允许多条（每条裁定 update 都是 final，
durability 权威语义），但必须满足：
  a. 一旦某任务发生过 final，后续**相邻同值**裁定不重复发布（`verdicts_emitted`
     尾部去重——隔位回归值不误伤，durability
     `test_verdict_order_and_repeated_values_across_restart` 要求
     `['通过','不通过','通过']` 全部以 final 发布）；
  b. **终态 final 单源（N2 限定版）**：对**同一终态时点**（同一 completed TASK_EVENT
     或同一终态 TASK_STATE 对应的账本位置），事件路径 final 与 synth 路径 final
     至多发布一条 final；冲突时以账本原序先到者为准，后到者降级为非 final 并记
     `final_source`。**裁定 update 的 final 不参与本条降级**，其发布遵循账本原序
     与 INV-4a。

### INV-5 重复与损坏 → 审计
重复 seq、非法 seq、非法裁定：产生 bridge_error（带 uai/seq/uai/kind），
且 bridge_error 本身幂等（synth 键入 state）。不得静默吞（契约 §11）。

### INV-6 幂等三元组
`(ledger_committed_offset, outbox 行数, state.tasks)` 三者一致才认为桥健康；
不一致 → fail-closed（拒绝处理新记录，要求人工 repair），禁止用回读悄悄对齐。

### INV-7 flush 是重放不是旁路
`force_pending_flush` = 把 `waiting_result` 任务**解除等待并重放**处理循环；
不存在与 drain 不同的第二条路由。flush 后 state 照常更新（含 INV-2 的挂起清除）。
"指针可能永不到达"由操作方显式决定，不是桥的默认行为。

### INV-8 指针迟到补发
解除挂起时，artifact_update 的 `uai/seq` 保留原 result_ready 的 seq；
若 completed 先于 artifact 发布（操作方 flush 过），artifact 仍按其原 seq 补发——
顺序由 INV-1 的账本序约束，消费方按 seq 重排。

---

## 4 处理循环（伪代码，唯一路由）

定义 `resolved(tid)`：`not state.tasks[tid].waiting_result
or state.tasks[tid].result_pointer is not null`。

```
process(batch):                       # batch = 账本新记录，按偏移序
  for rec in batch:
    absorb(rec)                       # 状态/指针/裁定 → state（不产生对外事件）
    if rec is TASK_EVENT:
      if invalid_seq:      emit bridge_error(INV-5); continue
      if duplicate_seq:    emit bridge_error(INV-5); continue
      if kind == result_ready and has_pointer: resolve(tid)   # 事件自带指针也解除挂起
      if kind == result_ready and no_pointer:
          state.tasks[tid].waiting_result = true
          continue                        # 记录已入账本序，只是不发布（INV-3）
      if task.waiting_result and not resolved(tid):
          continue                        # 已挂起：吸收但不发布（INV-3）
      ev = project(rec, state)            # 唯一投影入口
      emit(ev)
    elif rec is VERDICT:
      if invalid:          emit bridge_error(INV-5); continue
      if task.waiting_result and not resolved(tid):
          record only（发布等待解除后重放：按账本原序、按 INV-4 决定 final）
      elif same_as_last_emitted: skip
      else:               emit verdict update（INV-4 规则）
    elif rec is TASK_STATE:
      if task.waiting_result and not resolved(tid):
          record only（synth final 等解除后重放，INV-3 —— N3 守卫）
      elif terminal and no event-path final at this terminal slot:
          emit synth final（INV-4）
      # 注意：不预取未来，不重排（INV-1）
```

要点：**没有 pending 列表**。挂起是 state 里的一位布尔 + 账本序本身，
解除挂起 = 从上次发布位置重放账本（INV-3/INV-8）。

---

## 5 幂等与消费方契约

* 出站信箱 at-least-once：重复行以 `(task_id, uai/seq, uai/synth)` 键去重（消费方责任）；
* 桥自身 write 幂等保留（`_written` 语义并入 state）；
* `bridge_error` 事件消费方必须落审计，不得忽略。

## 6 崩溃窗口恢复

| 崩溃点 | 现象 | 恢复 |
|---|---|---|
| write 前 | 无输出 | 无损 |
| write 后、state 更新前 | outbox 有行、state 未提交 | 启动校验（INV-6）发现不一致 → 以 outbox 为准重放 state（事件已发布的事实优先），再继续 |
| state 更新后 | 一致 | 无损 |
| outbox 追加中途崩溃 | 尾部存在不完整 JSON 行 | 启动校验发现 → fail-closed（`incomplete_outbox_line`），要求人工修复；禁止静默丢弃或自动续写（N1） |

INV-6 的 fail-closed 是人工修复入口，不是自动对齐。

## 7 测试矩阵（实现验收 = 此矩阵全绿 + 三轮复核反例全绿）

1. 三轮复核的**全部**反例（r1/r2/r3 原样保留为回归）；
2. 组合矩阵：{result_ready 缺席指针}×{completed/failed/cancelled}×{0/1/N 条裁定}
   ×{同批/跨批}×{重启}×{flush}×**{≥2 任务交错}**（r2/r3 的 P1 反例均依赖交错），
   全部组合断言：无丢、无重、序正确；
3. 崩溃注入：write 后 state 更新前 kill（TMP 文件模拟），重启后 INV-6 行为正确；
4. 长账本增量（真账本 2,116 条）投影与 v1 产出比对（允许顺序修正的差异逐条列出）；
5. **撕裂行重启**（N1）：outbox 半行 → fail-closed；
6. **flush 后真指针到达**：INV-8 补发不产生重复 artifact；
7. **INV-6 不一致负路径**：人为破坏三元组一致性 → fail-closed 拒绝处理新记录，
   而非自动对齐。

## 8 迁移与门禁

* v1 代码保留为 `event_bridge_v1.py` 参考实现（不删除），v2 为 `event_bridge.py`；
* `experimental_test_only` 门禁**继续生效**，直到核验席对 v2 出具 PASS/PASS_WITH_NOTES；
* 真账本（2,116 条）投影作为迁移验收：v2 输出须与 v1 在"无丢无重"上等价。

---

## 9 核验记录

**SPEC_PASS_WITH_NOTES**（`RE-spec-reviewer-evbridge-v2-20261113.md`，E2 静态审读）。
七类历史反例在不变量层面全部锁死；三处文字缺口 N1（§6 撕裂行恢复行）、
N2（INV-4b 限定"同一终态时点"，裁定 update final 不参与降级）、N3（§4 TASK_STATE
挂起守卫）与 §4 四点修正（resolved 定义、事件自带指针解除挂起、VERDICT record-only
重放措辞、TASK_STATE 守卫）**已全部并入本 v2**。
实现授权：**已获**（条件=按本 v2 实现 + §7 矩阵含 4 项补充全绿 + 三轮反例回归全绿 +
门禁保持）。
