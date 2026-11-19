# UAI 团队记忆协议（席位必读）

> 生效：2026-11-20｜实现：`stage2/memory_*` + `stage2/uai_memory.py`
> 设计：`docs/UAI-MEMORY-PIPELINE-DESIGN.md`

## 为什么有这份协议

在此之前，「谁该写团队记忆」靠自觉：只有主动调图谱 API 的会话会写，
其余席位的决定、失败、阻塞全部消失在各自上下文里。结果是团队记忆覆盖率长期偏低，
且 hub 早期写入的裁定记忆**没有实体、没有边、没有嵌入**，语义检索根本查不到。

本协议把写入变成协作流程的一部分，而不是可选动作。

## 五条硬规则

1. **接手前先检索**：任何跨席位任务开始前，先 `uai-memory recall "<任务关键词>"`。
   检索结果里 `invalid_at` 非空的事实是被取代的旧结论，不能作为依据。
2. **决定必须落地**：用户确认的产品/技术/商业决定，立即 `remember --kind decision`。
3. **里程碑要留证**：完成并通过验证的交付，`remember --kind milestone` 且 `--evidence-level E1` 必须带命令与退出码。
4. **失败阻塞不得静默**：失败、超时、卡死、放弃范围，`remember --kind failure|blockage`。
5. **交接前审计**：交接单完成前跑 `uai-memory audit`，把「有结论未入图」清零或写明原因。

**不入图**：心跳、进度、寒暄、未经核验的自报、任何含凭据的内容。

## 命令

```bash
# 写入（E1 必须带证据）
uai-memory remember --kind decision --subject "UAI 记忆管道" \
  --claim "用户决定采用完整企业级记忆管道" \
  --evidence-level E1 --evidence "命令 exit=0"

# 检索
uai-memory recall "UAI 记忆管道现在什么状态"

# 审计
uai-memory audit

# 运维：补发、重放死信
uai-memory flush
uai-memory replay --intent-id <id>
```

## 写入语义

| 情况 | 结果 |
|---|---|
| Graphiti 可用 | `path=graphiti`，抽取实体/关系/嵌入，可语义检索 |
| Graphiti 不可用 | 自动兜底 `path=neo4j`，`degraded=true`，**审计可见，需补抽取** |
| 两条都不可用 | 留在 outbox 重试（1/2/4/8/16s），5 次后进死信 |
| 内容像凭据 | 入队即拒收；出队再扫一次，绝不写出 |

## 席位写入的真实机制（2026-11-20 补）

席位跑在只读沙箱里，不能写共享 outbox。所以记忆不是席位写进去的，是中枢代写的。

席位要做的只有一件事：在结果正文里输出记忆块（双方括号 MEMORY_INTENT，见 stage2/memory_bridge.py）。

- 中枢在任务 result_ready 时解析并代写，source_agent 记为席位；
- 解析失败或校验不过 → 账本 MEMORY_REJECTED，不静默；
- 任务契约写了 memory_intent_required: true 却没给块 → 裁定被打回 memory_intent_missing。

读取侧不需要席位自觉：派单时会自动注入【团队记忆·自动注入】...【注入结束】。

## 证据等级（沿用全队口径）

- **E1**：命令输出、退出码、hash、URL 响应；
- **E2**：另一 agent 复核并附复现步骤；
- **E3**：自证，必须标注「未独立验证」；
- **E4**：计划验证。

E3 记录不得用于对外验收结论。

## 审计口径

```text
覆盖 N/M 任务有记忆
```

- 只有 `status=delivered` 的 intent 才算「有记忆」；
- pending / retry / inflight / dead_letter 都算缺口，审计会点名；
- `degraded` 条目虽然写进了图，但**不可语义检索**，属于待补。
