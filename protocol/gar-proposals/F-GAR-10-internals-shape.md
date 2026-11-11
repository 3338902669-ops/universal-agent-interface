# F-GAR-10：统一 `internals: true` 与只读 facade 的形状

> 本文件是给 `governed-agent-runtime` 的**修复提案**，不是 UAI 实现。
> 针对修订：`3338902669-ops/governed-agent-runtime` @ `c754bbf`。
>
> **编号说明**：本文件名按 UAI 交接书 C-10 使用 `F-GAR-10`，与 GAR
> `KNOWN-FINDINGS.md` 的 **F-GAR-10** 是同一项（`internals: true` 交出原始对象，
> `ledger.rows` 在 facade 上是函数、在原始对象上是数组）。

## 问题复述

`createControlPlane` 按 `opts.internals === true` 二选一交出表面：

```js
const ledgerSurface = exposeInternals ? ledger : readOnlyLedger(ledger);
```

只读 facade（`readOnlyLedger`）里：

- `rows` 是函数：`() => clone(l.rows)`
- 另有 `verifyChain`、`fact`、`evidence`、`verifications`、`executions`、`grantSummary`
- **没有** `issueGrant`

原始 `Ledger` 实例上：

- `rows` 是数组
- `issueGrant` / `consumeGrant` / `recordExecution` 等变异方法可直接调用

同一字段两种形状。独立核验席曾经为此写过 workaround（F-GAR-10）。
`internals: true` 是测试缝，用来模拟「进程已被攻破」，但核验代码和审计代码
不应为了读 `rows` 先判断自己拿到的是数组还是函数。

`stats()` 也有同类问题：`ledgerRows: ledger.rows.length` 依赖数组形状。

## 最小修复（针对 governed-agent-runtime）

**始终**交出与 `readOnlyLedger` 相同的只读键（`rows` 永远是函数）。
`internals: true` 只在同一 facade 上**附加**特权方法，并提供显式的 `raw()`
给真正需要底层对象的测试。不要再把原始实例直接当作 `api.ledger`。

兼容层：若旧测试写了 `cp.ledger.rows.length` 或 `Array.isArray(cp.ledger.rows)`，
在特权 facade 上给 `rows` 函数加 `length` 不能可靠地模拟数组。正确做法是：

- 新代码一律 `cp.ledger.rows()`。
- 旧代码可通过 `cp.ledger.raw().rows`（数组）访问；`raw()` 仅在 `internals: true` 存在，
  runner 表面没有这个方法。

```diff
--- a/src/control-plane.mjs
+++ b/src/control-plane.mjs
@@
   function readOnlyLedger(l) {
     return Object.freeze({
       verifyChain: () => l.verifyChain(),
       rows: () => clone(l.rows),
       fact: (key) => l.fact(key),
       evidence: () => clone([...l.evidence.values()]),
       verifications: () => clone([...l.verifications.values()]),
       executions: () => clone([...l.executions.values()]),
       grantSummary: () => ({
         issued: l.grants.size,
         consumed: [...l.grants.values()].filter(function (g) { return g.consumedAt !== null; }).length,
       }),
     });
   }
+
+  function privilegedLedger(l) {
+    return Object.freeze({
+      ...readOnlyLedger(l),
+      issueGrant: (input) => l.issueGrant(input),
+      peekGrant: (id) => l.peekGrant(id),
+      consumeGrant: (id, attempt) => l.consumeGrant(id, attempt),
+      recordExecution: (input) => l.recordExecution(input),
+      recordEvidence: (input) => l.recordEvidence(input),
+      raw: () => l,
+    });
+  }
+
+  function privilegedRegistry(r) {
+    return Object.freeze({
+      ...readOnlyRegistry(r),
+      register: (input) => r.register(input),
+      assertIntegrity: (id) => r.assertIntegrity(id),
+      checkIntegrity: (id) => r.checkIntegrity ? r.checkIntegrity(id) : undefined,
+      raw: () => r,
+    });
+  }
@@
-  const ledgerSurface = exposeInternals ? ledger : readOnlyLedger(ledger);
-  const registrySurface = exposeInternals ? registry : readOnlyRegistry(registry);
+  const ledgerSurface = exposeInternals ? privilegedLedger(ledger) : readOnlyLedger(ledger);
+  const registrySurface = exposeInternals ? privilegedRegistry(registry) : readOnlyRegistry(registry);
@@
-    stats: () => ({ runtime: runtime.stats(), ledgerRows: ledger.rows.length }),
+    stats: () => ({ runtime: runtime.stats(), ledgerRows: ledgerSurface.rows().length }),
```

Registry / memory / governance 做同样处理：公共键集合与只读 facade 对齐，特权方法另加，
`raw()` 仅 internals 可见。

## 验收测试

1. **形状一致**：无论 `internals` 真或假，`typeof cp.ledger.rows === 'function'`，
   `Array.isArray(cp.ledger.rows())`，且两次得到的是 clone（改返回值不影响账本）。
2. **特权不泄漏**：`internals: false` 时 `cp.ledger.issueGrant === undefined` 且
   `cp.ledger.raw === undefined`；既有 A16 攻击（用公开 ledger 铸造 grant 再 execute）
   仍然失败。
3. **特权仍可用**：`internals: true` 时测试可 `cp.ledger.issueGrant(...)`，也可
   `cp.ledger.raw().rows` 拿到底层数组；adversarial 用例不必改意图。
4. **键集合**：把 `Object.keys(readOnlyLedger(l))` 作为公共键的下限，特权 facade
   必须包含全部公共键，且同名键的 typeof 相同。
5. **stats**：`cp.stats().ledgerRows` 在两种模式下都等于当前行数，不抛 TypeError。
6. 核验代码用 `rows()` 遍历时，不再需要 `Array.isArray(x.rows) ? x.rows : x.rows()` 这种分支。

## 不修的风险

- 独立核验席会继续为同一字段写两种访问路径，复跑脚本脆弱，误报难以归因。
- 任何文档若写「`ledger.rows` 是只读列表」，在 `internals: true` 的测试世界里是错的；
  反过来，写「`rows` 是数组」又会在 runner 表面上崩掉。
- 形状差异本身不是安全漏洞，但它会掩盖真漏洞：核验代码一旦为了迁就形状写出
  特殊分支，就可能在一种模式下跳过检查。
