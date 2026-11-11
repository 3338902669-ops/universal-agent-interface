# F-GAR-08：把 `authorize()` 收成纯函数（检测与吊销分离）

> 本文件是给 `governed-agent-runtime` 的**修复提案**，不是 UAI 实现。
> 针对修订：`3338902669-ops/governed-agent-runtime` @ `c754bbf`。
>
> **编号说明**：本文件名按 UAI 交接书 C-08 使用 `F-GAR-08`。它对应的是
> GAR `KNOWN-FINDINGS.md` 的 **F-GAR-07**（`assertIntegrity` 在 gate 求值时吊销），
> **不是** 现有的 F-GAR-08（审批身份未认证，UAI A-01/C-03 / Orchestra A4）。
> 合入时建议关闭或改写 F-GAR-07。

## 问题复述

`authorize(ctx)` 被说成「对上下文的判定」。实际上策略规则 P07b 会调用
`ctx.registry.assertIntegrity(ctx.session.artifactId)`，而
`ArtifactRegistry.assertIntegrity` 在哈希不匹配时**当场改写** registry：

- `record.trust = 'REVOKED'`
- `record.revokedAt` / `record.revokedReason` 被写入
- `this.#log('integrity_violation', …)` 追加事件
- 再抛 `ARTIFACT_TAMPERED`

于是一次「只是问问能不能做」的 gate 求值会杀死同一 artifact 上的其他 session。
处置（F-GAR-07）承认吊销是幂等的、且「检测后继续放行」更糟，但因此
「pure gate」是过声称：`authorize()` 不是其输入的纯函数。

副作用本身可以保留，但必须移出 `authorize()`。

## 最小修复（针对 governed-agent-runtime）

把完整性检查拆成两个函数：

- `checkIntegrity(artifactId)`：只读，返回 `{ ok, expected, recomputed }`，不改 registry。
- `revokeForIntegrity(artifactId, reason)`：唯一写入路径，负责 trust=REVOKED、日志、后续 session 失效。

P07b 只调用 `checkIntegrity`。`authorize()` 对同一份冻结快照多次调用必须得到同一份 receipt，且不改变 `registry` / `ledger`。
若 receipt 为 `BLOCK/ARTIFACT_TAMPERED`，由 **control plane 在 authorize 返回之后** 调用 `revokeForIntegrity`。

```diff
--- a/src/identity.mjs
+++ b/src/identity.mjs
@@
-  assertIntegrity(artifactId) {
+  checkIntegrity(artifactId) {
     const record = this.current(artifactId);
     const recomputed = hashOf({
       artifactId: record.artifactId,
       name: record.name,
       version: record.version,
       roleId: record.roleId,
       instructions: record.instructions,
       tools: record.tools,
       policies: record.policies,
       memorySchema: record.memorySchema,
     });
-    if (recomputed !== record.definitionHash) {
+    if (recomputed !== record.definitionHash) {
+      return { ok: false, artifactId, expected: record.definitionHash, recomputed };
+    }
+    return { ok: true, artifactId, expected: record.definitionHash, recomputed };
+  }
+
+  revokeForIntegrity(artifactId, reason) {
+    const record = this.current(artifactId);
+    if (record.trust === 'REVOKED' && record.revokedReason === reason) return clone(record);
       record.trust = 'REVOKED';
-      record.revokedAt = 'integrity-check';
-      record.revokedReason = 'content edited in place: stored hash ' + record.definitionHash + ' != recomputed ' + recomputed;
-      this.#log('integrity_violation', { artifactId, expected: record.definitionHash, recomputed });
-      throw new Refusal('ARTIFACT_TAMPERED', 'artifact content does not match its definition hash');
-    }
-    return true;
+    record.revokedAt = 'integrity-check';
+    record.revokedReason = reason;
+    this.#log('integrity_violation', { artifactId, reason });
+    return clone(record);
   }
+
+  assertIntegrity(artifactId) {
+    const check = this.checkIntegrity(artifactId);
+    if (!check.ok) {
+      this.revokeForIntegrity(artifactId,
+        'content edited in place: stored hash ' + check.expected + ' != recomputed ' + check.recomputed);
+      throw new Refusal('ARTIFACT_TAMPERED', 'artifact content does not match its definition hash');
+    }
+    return true;
+  }
```

```diff
--- a/src/policy.mjs
+++ b/src/policy.mjs
@@
     id: 'P07b',
     invariant: 'I14',
     code: 'ARTIFACT_TAMPERED',
     when(ctx) {
       if (!ctx.session) return null;
-      try {
-        ctx.registry.assertIntegrity(ctx.session.artifactId);
-        return null;
-      } catch (error) {
-        if (!error || error.code !== 'ARTIFACT_TAMPERED') throw error;
-        return decision('BLOCK', 'ARTIFACT_TAMPERED',
-          'artifact ' + ctx.session.artifactId + ' does not match its definition hash: ' + String(error.message), 'I14');
-      }
+      const check = ctx.registry.checkIntegrity(ctx.session.artifactId);
+      if (check.ok) return null;
+      return decision('BLOCK', 'ARTIFACT_TAMPERED',
+        'artifact ' + ctx.session.artifactId + ' does not match its definition hash: stored '
+        + check.expected + ' != recomputed ' + check.recomputed, 'I14');
     },
   },
```

```diff
--- a/src/control-plane.mjs
+++ b/src/control-plane.mjs
@@
     const receipt = authorize(ctx);
+    if (receipt && receipt.code === 'ARTIFACT_TAMPERED' && ctx.session) {
+      const check = registry.checkIntegrity(ctx.session.artifactId);
+      if (!check.ok) {
+        registry.revokeForIntegrity(ctx.session.artifactId,
+          'content edited in place: stored hash ' + check.expected + ' != recomputed ' + check.recomputed);
+        ledger.append('trust.revoked', {
+          artifactId: ctx.session.artifactId,
+          reason: 'integrity-check',
+          by: 'gate-post-authorize',
+          at: clock(),
+        });
+      }
+    }
     // ... existing allow / refuse handling ...
```

`assertIntegrity` 可保留为「检查 + 吊销」的兼容包装，但 **policy / authorize 不得再调用它**。

## 验收测试

1. **纯性**：对已篡改的 artifact 连续调用两次 `authorize(ctx)`（中间不走 control plane）→ 两次 receipt 字节级相同，且 `registry.current(...).trust` 在两次调用后仍未被改写。
2. **仍会吊销**：走 `submit` / `decide` 的 control-plane 路径后，同一 artifact 上的其他 session 再提交必须 `TRUST_REVOKED` 或 `ARTIFACT_TAMPERED`。
3. **幂等吊销**：对已因完整性问题吊销的 artifact 再走一次 gate，不得抛错，不得重复破坏账本链。
4. **非篡改错误**：`checkIntegrity` 遇到未知 artifact 等非 `ARTIFACT_TAMPERED` 条件，行为与今日一致，不得被误标为篡改。
5. **既有 invariants.test.mjs** 里 `assert.throws(() => world.cp.registry.assertIntegrity(artifactId), … ARTIFACT_TAMPERED)` 仍然通过（兼容包装保留）。
6. 不得把「检测后继续 ALLOW」当作一种合法路径。

## 不修的风险

- 「pure gate」会继续是过声称：只是读一次授权，就会改全局信任状态，测试和审计都无法把判定与副作用分开复现。
- 未来若有只读「试探性授权」（例如预检、模拟、审计回放），一跑就会误吊销。
- 并发下两次 authorize 交错写入 registry，比「先判定、后由单一路径吊销」更难推理。
