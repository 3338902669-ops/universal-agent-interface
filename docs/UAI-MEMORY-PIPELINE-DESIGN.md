# UAI 企业级团队记忆管道（第 3 档）设计规格

> 状态：已获用户 2026-11-20 确认（写路径 C / SQLite Outbox / 本期不做集群）
> 作者：DSH lead（实现者）｜核验者必须非实现者
> 仓库：https://github.com/3338902669-ops/universal-agent-interface

## 1. 背景与实测缺陷（E1）

真实图库直查（2026-11-20）：

| 写入路径 | 事实数 | 关系数 | 向量嵌入 | group_id | 语义可检索 |
|---|---:|---:|---:|---|---|
| Graphiti HTTP `/episodes` | 正常 | 有 | 有 | `team` | 是 |
| Hub `outcome_recorder` 直写 Neo4j | 4 | **0** | **0** | **`neo4j`** | 否 |

证据：

```text
NAME= unknown task outcome
  GID= neo4j  SRC= uai.hub.on_verdict
  HAS_EMBED= False
UAI_EPS= 4   RELS= 0
ENTITIES= 1998
```

结论：**「裁定已回写图谱」在技术上成立，但在「团队记忆可被检索、可被推理」这个业务目标上不成立。**

## 2. 目标

1. 六类结构化记忆可靠入图，可语义检索；
2. 图谱不可用时不丢记忆（持久化 outbox + 重试 + 死信）；
3. 写后回读校验，写入结果分三态：verified / unverified / failed；
4. 审计能回答「哪些有结论但没入图」；
5. 四席位统一入口，不各自记 HTTP 细节。

## 3. 非目标

- 不做全量聊天自动灌入；
- 不做跨机分布式 outbox；
- 不重写 Graphiti 抽取；
- 图谱不是权威，append-only 账本仍是权威。

## 4. 架构

```text
Agent 协议层  →  uai-memory remember|recall|audit|flush
意图层        →  MemoryIntent（结构化，非自由文本）
投递层        →  SQLite Durable Outbox（状态机 + 退避 + 死信）
写入层        →  主 Graphiti /episodes ；兜底 Neo4j 直写（degraded=true）
校验审计层    →  写后回读 + 每日审计 + 死信报表
```

## 5. 写路径（方案 C）

1. 主路径：Graphiti `POST /episodes`（抽取实体 + 关系 + 嵌入）。
2. 兜底：Neo4j `MERGE (e:Episodic {uai_intent_key})`，**必须** `degraded=true`、`group_id='team'`。
3. 幂等：`intent_id` 唯一 + 图属性 `uai_intent_key`。
4. 兜底写入在审计中必须可被列出，可批量补抽取。

## 6. MemoryIntent 契约

必填：`intent_id`、`kind`、`subject`、`claim`、`evidence_level`、`reference_time`。

`kind` ∈ {decision, milestone, failure, blockage, handoff, verdict}
`evidence_level` ∈ {E1, E2, E3, E4}；**E1 必须带 evidence**（命令/退出码/哈希摘要）
`sensitivity` ∈ {public, internal}；`secret` 直接拒收
`group_id` 默认 `team`，匹配 `^[a-z0-9_-]{1,32}$`
凭据样式文本（api_key / password / secret / token=）**入队拒收**

## 7. Outbox 状态机

```text
pending → inflight → delivered
                  ↘ retry（退避 1/2/4/8/16s，最多 5 次）→ dead_letter
```

- SQLite WAL；`inflight` 超过 120s 视为崩溃，回退 `retry`；
- 单 writer 串行 drain；
- 死信可 `replay`。

## 8. 审计输出

```text
有结论未入图：task_id / 声明时间 / 证据等级 / 缺失原因
降级写入：degraded=true 条数（不可语义检索，需补抽取）
死信：条数 + 最后错误
覆盖：N/M 任务有记忆
```

## 9. Hub 集成

- verdict → 入队 `kind=verdict`（保留现有语义，改为写 outbox）；
- `TIMED_OUT` / `FAILED` → `kind=failure`；
- lease 丢失 / worker 崩溃 → `kind=blockage`；
- **`accepted` / `heartbeat` 不入图**（防噪音）。

## 10. 验收（必须可执行）

| 阶段 | 验收命令 | 通过标准 |
|---|---|---|
| P1 | `python stage2/test_memory_intent.py` | 全部 PASS |
| P2 | `python stage2/test_memory_outbox.py` | 全部 PASS |
| P3 | `python stage2/test_memory_writer.py` | 全部 PASS |
| P4 | `python stage2/test_memory_audit.py` | 全部 PASS |
| P5 | `python stage2/test_uai08_graph_write.py` | 不回归 |
| P6 | 真图谱：`uai-memory remember` + `/search` 命中 | E1 |


## 12. 独立审查（第一轮）与修复

审查者：`memory-review`（非实现者，只读）。结论 FAIL，5 类 P1。全部已修并加回归。

| # | 缺陷 | 修法 | 回归 |
|---|---|---|---|
| 1 | 凭据只扫 3 个字段，refs/session 可绕过；标记表过窄且误杀 "secret sauce" | 正则标记表 + 递归扫全部字符串叶子；入队即拒收 | H-1..H-9 |
| 2 | outbox 并发 claim 重复领取；mark_failed 能把 delivered 回滚 | BEGIN IMMEDIATE + 条件更新；mark_failed 仅对 inflight 生效 | H-10..H-13 |
| 3 | 兜底写入自证 verified；审计把未确认算成有记忆 | 兜底路径回读返回 None；覆盖只认 verified | H-14..H-17 |
| 4 | 终态记忆异常被吞、账本无痕；裁定路径等图谱 3s | 裁定只入队(flush=False)；后台任务持强引用并写 MEMORY_OUTCOME | H-18..H-21 |
| 5 | 新路径写 uai_intent_key，旧工具查 uai_key → 写入后检索不到 | 写入后补两套键 + group_id | 真图核验 |

### 语义变更（相对初版设计）

1. **裁定路径不再同步写图**：`on_verdict` 只把 intent 落进 outbox（毫秒级），响应带 `status=pending`，投递由后台 drain 完成。原因是 Graphiti 冷启动实测 4.8s，同步等待会让每次裁定固定吃满超时。
2. **审计覆盖率口径收紧**：只有 `delivered` 且 `verified='verified'` 才算有记忆；`unverified_write` 单列为缺口。
3. **幂等键确定化**：`verdict:<task_id>:<verdict>`、`terminal:<task_id>:<state>:<reason>`，hub 重投不会产生第二条记忆。


## 13. 独立审查（第二轮）与修复

审查者同一人（非实现者）。结论 FAIL，1 类 P1 + 4 类 P2。全部已修并加回归（H-27..H-36）。

| # | 缺陷 | 修法 | 回归 |
|---|---|---|---|
| 1 | Graphiti `/episodes` 非幂等（实测同 payload 两次 → 2 个 Episodic），崩溃重放会写重复记忆 | 写前按 name/intent_key 查重，已存在则跳过投递并标记 `already_present` | H-22..H-26、H-35/36 |
| 2 | `mark_delivered` 在行被 recover_stale 打回后静默失效，drain 仍计 delivered | `mark_delivered` 返回 rowcount；未落库计 retried | H-30/31 |
| 3 | 凭据扫描有 `depth > 5` 截断，深层 refs 可绕过并落盘 | 改显式栈遍历（无深度上限 + 环检测）；拒写的行 purge 清空 payload | H-27..H-29 |
| 4 | `PRAGMA journal_mode=WAL` 在写锁下立即抛 database is locked；入队直接失败丢记忆；deferred 也写 MEMORY_OUTCOME 造成噪音 | 先设 busy_timeout 再切 WAL 且失败可容忍；入队 3 次退避重试；deferred 不再记账本 | H-32..H-34 |
| 5 | `outcome_recorder` 已是死路径但文档/测试仍当生产写入点 | 标弃用并改文档；旧测试只保留其自身契约 | DEVELOPMENT.md |

## 14. 生产实跑（E1，2026-11-20）

重启真实 hub/四席后，对历史卡死任务 `96613a7d`（TIMED_OUT）经 NATS 发真实裁定：

```text
VERDICT_RESPONSE graph_memory = {enqueued: true, status: "pending",
                                 intent_id: "verdict:96613a7d-...:不通过"}
25s 后 outbox 行 = delivered / path=graphiti / verified=verified
audit 覆盖 0/62 → 1/62（本次裁定被计入）
```

这条链证明了设计里最关键的一点：**裁定不等图谱**（立即返回 pending），
后台 drain 完成实体/关系/嵌入抽取，审计按 verified 计数。


## 15. 独立审查（第三轮）与修复（达三轮冻结线）

第三轮仍 FAIL：1 P1 + 4 P2/P3。已修；按治理规则**不再开第四轮审查**，改为冻结上报。

| # | 缺陷 | 修法 | 回归 |
|---|---|---|---|
| 1 | 兜底 stub 被 `_exists` 当成已写入 → 降级记忆永不升级、degraded 从审计消失；`_exists` 未按 group 过滤 | `_exists` 只认「非 degraded + 同 group」；主路成功后删除同 intent 的 stub | H-25/26 + 真图探针 |
| 2 | 写前查重是 TOCTOU：写窗口内被回收/重放仍可双投；`replay` 可把 inflight 打回 | `replay` 仅允许 dead_letter/retry/pending；并发窗口作为已知边界记录 | H-37/38 |
| 3 | 同一裁定重复提交被报成记忆写丢 | duplicate 计入 deferred，不写 MEMORY_OUTCOME | H-39 |
| 4 | purge 只逻辑清空，明文仍在库/WAL | `PRAGMA secure_delete=ON` + purge 后 checkpoint + VACUUM | H-40/41 |
| 5 | 入队重试预算（3×5s）超出裁定等待窗口；WAL 切换失败静默 | 入队 2 次 × 1s busy_timeout；WAL/secure_delete 失败记 WARNINGS | H-42/43 |

### 真图探针（E1）

```text
stub_write → {'ok': True}
exists_after_stub → False          # stub 不再被当成已写入
write_intent → path=graphiti, degraded=False, verified=verified
nodes_after_upgrade → [{'deg': None, 'g': 'team'}]   # stub 已删除，只剩 1 条
```

### 已知残留边界（不再本轮修，明确记录）

- **多进程并发写同一 intent**：查重与写入之间的窗口在跨进程下仍存在，真正解需要图侧唯一约束或确定性 uuid。当前架构下写者只有 hub 单进程 + 人工 CLI，实际风险低。

## 11. 风险

| 风险 | 处置 |
|---|---|
| 抽取 LLM 慢/失败 | 兜底写入 + degraded + 补抽取 |
| 记忆噪音 | 只允许 6 类 intent |
| 密钥入库 | 入队拒收 + 出队二次扫描 + 测试 |
| 双路径写重 | intent_id 幂等 |
| 图谱成隐性权威 | 账本权威，图谱派生 |
