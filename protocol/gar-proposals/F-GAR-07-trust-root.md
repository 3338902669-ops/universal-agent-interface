# F-GAR-07：把信任根从「单人、gate 外」收成双人 bootstrap

> 本文件是给 `governed-agent-runtime` 的**修复提案**，不是 UAI 实现。
> 针对修订：`3338902669-ops/governed-agent-runtime` @ `c754bbf`。
>
> **编号说明**：本文件名按 UAI 交接书 C-07 使用 `F-GAR-07`。它对应的是
> GAR `KNOWN-FINDINGS.md` 的 **F-GAR-04**（「The root of trust is outside the
> gate」/`bootstrapTrust`），**不是** 现有的 F-GAR-07（`assertIntegrity` 副作用；
> 见 `F-GAR-08-pure-authorize.md`）。合入时建议直接关闭或改写 F-GAR-04。

## 问题复述

`src/control-plane.mjs` 的 `bootstrapTrust` 是全系统唯一一次不上 gate 的状态变更：
一个具名人类主体即可对种子 artifact 做 evaluate + release，账本只追加一条
`trust.bootstrap`。这是必须存在的根操作——没有「更上面的 agent」——但当前实现是
**单人、单事件、无第二签名**。

后果：

- 任何一个能调用 operator 表面的人（或拿到 `internals: true` 的测试缝）可以单独
  建立整条信任链的根。
- 账本无法区分「两名独立人类同意放行」与「同一人写了两次」。
- 测试夹具 `fixture.mjs` 对每个角色 artifact 都由 `'human-root'` 一人完成 bootstrap，
  把单人根变成了默认路径。

F-GAR-04 的 disposition 是「accept with disclosure」。本提案不取消 ungated 根操作，
只把它收成**双人 bootstrap**：两次独立签名 + 两条可审计事件，根仍然在 gate 外，
但不再是单点。

## 最小修复（针对 governed-agent-runtime）

协议：

1. `bootstrapTrust` 拆成两步，或接受一份含两个不同 `by` 的请求。
2. 第一步 `nominate`：人类 A 对 artifact 做评价，账本追加
   `trust.bootstrap.nominated`，artifact 仍为 `UNKNOWN` / 未 release。
3. 第二步 `confirm`：人类 B（`B !== A`）对**同一 `definitionHash`** 签名确认，
   账本追加 `trust.bootstrap.confirmed`，这时才允许 `evaluate` 生效并 `release`。
4. 任一步单独出现都不改变可执行信任（不得进入 `VALIDATED` / `TRUSTED`）。
5. 两人签名都记录 `by`、`at`、`definitionHash`；中间若 artifact 被 mutate，
   第二步必须失败（hash 已变）。
6. Agent 角色仍然不能调用该表面（保持 `operatorOnly` / `OPERATOR_SURFACE_REQUIRED`）。

```diff
--- a/src/control-plane.mjs
+++ b/src/control-plane.mjs
@@
   function bootstrapTrust(input) {
-    const record = registry.evaluate({
-      artifactId: input.artifactId,
-      by: input.by || 'human-principal',
-      byRole: 'human',
-      checks: input.checks && input.checks.length ? input.checks : [{ name: 'human-root-review', passed: true }],
-      at: clock(),
-    });
-    ledger.append('trust.bootstrap', {
-      artifactId: input.artifactId,
-      by: input.by || 'human-principal',
-      definitionHash: record.artifact.definitionHash,
-    });
-    let released = null;
-    if (input.release !== false) {
-      released = registry.release({ artifactId: input.artifactId, by: input.by || 'human-principal', at: clock() });
-      ledger.append('artifact.released', { artifactId: input.artifactId, by: input.by || 'human-principal', bootstrap: true });
-    }
-    return { evaluation: record.evaluation, artifact: released || record.artifact };
+    const artifactId = input.artifactId;
+    const current = registry.current(artifactId);
+    const principal = input.by;
+    if (!principal) throw new Refusal('BOOTSTRAP_PRINCIPAL_REQUIRED', 'bootstrap requires a named human principal');
+
+    if (input.phase === 'nominate' || !input.phase) {
+      if (current.bootstrap && current.bootstrap.nominatedBy) {
+        throw new Refusal('BOOTSTRAP_ALREADY_NOMINATED', 'artifact already has a bootstrap nomination');
+      }
+      ledger.append('trust.bootstrap.nominated', {
+        artifactId, by: principal, definitionHash: current.definitionHash, at: clock(),
+      });
+      current.bootstrap = { nominatedBy: principal, nominatedHash: current.definitionHash, nominatedAt: clock() };
+      if (input.phase === 'nominate') return { phase: 'nominated', artifact: clone(current) };
+    }
+
+    const confirmBy = input.confirmBy;
+    if (!confirmBy) throw new Refusal('BOOTSTRAP_SECOND_PRINCIPAL_REQUIRED', 'confirm requires a second human principal');
+    if (confirmBy === current.bootstrap.nominatedBy) {
+      throw new Refusal('BOOTSTRAP_DUPLICATE_PRINCIPAL', 'the confirming principal must differ from the nominator');
+    }
+    if (current.definitionHash !== current.bootstrap.nominatedHash) {
+      throw new Refusal('BOOTSTRAP_HASH_CHANGED', 'artifact changed between nominate and confirm');
+    }
+    const record = registry.evaluate({
+      artifactId, by: confirmBy, byRole: 'human',
+      checks: input.checks && input.checks.length ? input.checks : [{ name: 'human-root-review', passed: true }],
+      at: clock(),
+    });
+    ledger.append('trust.bootstrap.confirmed', {
+      artifactId, by: confirmBy, nominatedBy: current.bootstrap.nominatedBy,
+      definitionHash: record.artifact.definitionHash, at: clock(),
+    });
+    let released = null;
+    if (input.release !== false) {
+      released = registry.release({ artifactId, by: confirmBy, at: clock() });
+      ledger.append('artifact.released', { artifactId, by: confirmBy, bootstrap: true });
+    }
+    return { evaluation: record.evaluation, artifact: released || record.artifact };
   }
```

测试夹具改为显式两人：`bootstrapTrust({ artifactId, by: 'human-root-a', phase: 'nominate' })` 再
`bootstrapTrust({ artifactId, confirmBy: 'human-root-b', phase: 'confirm' })`。不得再默认
`'human-principal'` 单人走完。

## 验收测试

1. **双人成功**：A nominate、B confirm（A≠B，hash 不变）→ 两条账本事件，artifact 可 release，后续 session 可执行。
2. **单人不足**：只 nominate，不 confirm → 信任仍为 `UNKNOWN`，`openSession` / `submit` 被 `TRUST_NOT_ESTABLISHED` 挡住。
3. **同一人两次**：`confirmBy === nominatedBy` → `BOOTSTRAP_DUPLICATE_PRINCIPAL`，不得 release。
4. **中间篡改**：nominate 之后 mutate artifact，confirm 必须 `BOOTSTRAP_HASH_CHANGED`。
5. **缺人名**：`by` 或 `confirmBy` 缺失 → 拒绝，不得回落到 `'human-principal'`。
6. **runner 表面**：`internals: false` 时调用 `bootstrapTrust` 仍抛 `OPERATOR_SURFACE_REQUIRED`（攻击 A16 / 扫描高危项不得回退）。
7. 既有 `trust.bootstrap` 单事件路径从生产 API 移除；如需兼容测试，只允许在显式 `legacy: true` 且测试构建中使用，并在账本标 `legacy: true`。

## 不修的风险

- 信任根仍是单点：一个人（或一段能碰到 operator 表面的代码）就能放出所有后续 grant 的前提。
- 审计只能看到「有人做了 bootstrap」，无法证明有过第二双眼睛。
- 与项目自己写的「唯一 ungated 状态变更必须被如实记录」并不矛盾，但「记录了」不等于「有制衡」；外部复评会把 F-GAR-04 继续标为未收口。
