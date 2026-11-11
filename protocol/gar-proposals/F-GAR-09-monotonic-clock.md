# F-GAR-09：grant TTL 改用单调时钟，测试可确定性保留在夹具层

> 本文件是给 `governed-agent-runtime` 的**修复提案**，不是 UAI 实现。
> 针对修订：`3338902669-ops/governed-agent-runtime` @ `c754bbf`。
>
> **编号说明**：本文件名按 UAI 交接书 C-09 使用 `F-GAR-09`。它对应的是
> GAR `KNOWN-FINDINGS.md` 的 **F-GAR-06**（grant TTL 建立在可注入的墙上时钟上），
> **不是** 现有的 F-GAR-09（安全扫描修复尚未被独立复验）。合入时建议关闭或改写 F-GAR-06。

## 问题复述

Grant 过期比较写在 `Ledger.consumeGrant`：

```js
const nowIso = this.#at(); // this.clock()，默认 new Date().toISOString()
if (grant.expiresAt && new Date(nowIso).getTime() > new Date(grant.expiresAt).getTime()) {
  throw new Refusal('GRANT_EXPIRED', ...);
}
```

`createGovernedRuntime` 用同一 `clock()` 计算 `expiresAt = clock() + grantTtlMs`。
`createControlPlane` / `makeWorld` 允许调用方注入 `clock`。`manualClock` 从一张
固定的 ISO 墙上时间起步，测试靠 `clock.advance(ms)` 让 grant 过期。

问题：

- 过期语义绑定的是**可被调用方替换的墙上时钟**。谁提供 clock，谁就控制「现在」。
- 墙上时钟会回拨；回拨后已过期的 grant 可能再次可用。
- 生产路径默认 `systemClock()` = `new Date().toISOString()`，同样不是单调源。
- 测试确实需要确定性，所以注入缝必须保留，但应下沉到夹具层，而不是让生产
  `consumeGrant` 相信调用方送来的「现在」。

## 最小修复（针对 governed-agent-runtime）

原则：**TTL 的真源是单调截止时间**；ISO `expiresAt` 只是签发时刻的可读投影。

- 生产时钟：`monoNs()` 用 `process.hrtime.bigint()`（或 `performance.now()`），
  再加一个进程启动时冻结的 wall origin，用于写 ISO 字段。
- Grant 同时存 `expiresAtMonoNs` 与 `expiresAt`（ISO）。`consumeGrant` 只比较
  `clock.monoNs() > grant.expiresAtMonoNs`。
- **注入缝只留在 `createControlPlane` / `makeWorld` 的测试夹具**：`manualClock`
  同时提供 `now()`（ISO）和 `monoNs()` / `advance(ms)`。生产 API 与 runner 表面
  不得接受调用方传入的 clock。
- `fixedClock` / 旧的「只返回 ISO 的函数」不再作为生产默认。

```diff
--- a/src/core.mjs
+++ b/src/core.mjs
@@
-export function systemClock() {
-  return new Date().toISOString();
-}
+export function systemClock() {
+  const originWall = Date.now();
+  const originMono = process.hrtime.bigint();
+  return {
+    now() {
+      const elapsedMs = Number(process.hrtime.bigint() - originMono) / 1e6;
+      return new Date(originWall + elapsedMs).toISOString();
+    },
+    monoNs() {
+      return process.hrtime.bigint();
+    },
+  };
+}
```

```diff
--- a/src/fixture.mjs
+++ b/src/fixture.mjs
@@
 export function manualClock(startIso) {
   let t = new Date(startIso || '2026-10-02T00:00:00.000Z').getTime();
+  const originMono = 0n;
   return {
     now: () => new Date(t).toISOString(),
+    monoNs: () => originMono + BigInt(t - new Date(startIso || '2026-10-02T00:00:00.000Z').getTime()) * 1000000n,
     advance(ms) { t += ms; },
   };
 }
```

```diff
--- a/src/runtime.mjs
+++ b/src/runtime.mjs
@@
-      const expiresAt = new Date(new Date(clock()).getTime() + grantTtlMs).toISOString();
+      const issuedMono = clock.monoNs();
+      const expiresAtMonoNs = issuedMono + BigInt(grantTtlMs) * 1000000n;
+      const expiresAt = new Date(new Date(clock.now()).getTime() + grantTtlMs).toISOString();
       const grant = ledger.issueGrant({
         // ... existing bindings ...
         expiresAt,
+        expiresAtMonoNs,
         runtimeId,
       });
```

```diff
--- a/src/ledger.mjs
+++ b/src/ledger.mjs
@@
   constructor(clock) {
-    this.clock = clock || function () { return new Date().toISOString(); };
+    this.clock = clock && typeof clock.now === 'function' ? clock : systemClock();
   }
   #at() {
-    return this.clock();
+    return this.clock.now();
   }
@@
   consumeGrant(grantId, attempt) {
     const grant = this.grants.get(grantId);
     if (!grant) throw new Refusal('GRANT_UNKNOWN', 'no such grant: ' + String(grantId));
     if (grant.consumedAt) throw new Refusal('GRANT_REPLAYED', ...);
-    const nowIso = this.#at();
-    if (grant.expiresAt && new Date(nowIso).getTime() > new Date(grant.expiresAt).getTime()) {
-      throw new Refusal('GRANT_EXPIRED', 'grant ' + grantId + ' expired at ' + grant.expiresAt);
-    }
+    const nowMono = this.clock.monoNs();
+    if (grant.expiresAtMonoNs != null && nowMono > grant.expiresAtMonoNs) {
+      throw new Refusal('GRANT_EXPIRED', 'grant ' + grantId + ' expired at ' + grant.expiresAt);
+    }
```

`createControlPlane` 把注入的 `opts.clock` 只在 `opts.internals === true` 或显式
`opts.allowClockInjection === true` 时接受；否则忽略调用方 clock，使用 `systemClock()`。
这样生产 runner 表面带不走时间。

既有测试 `world.clock.advance(120000)` / `advance(3600000)` 不必改语义：夹具的
`advance(ms)` 同时推进 `now()` 与 `monoNs()`。

## 验收测试

1. **真实过期**：`manualClock` 签发 TTL=30s 的 grant，`advance(30001)` 后 `consumeGrant` 抛 `GRANT_EXPIRED`。
2. **未到期**：`advance(29999)` 后仍可消费一次。
3. **墙钟回拨无效**：签发后把夹具的 *wall* 字段回拨（仅改 ISO，不改 `monoNs`）→ 已过单调截止的 grant 仍是 `GRANT_EXPIRED`。
4. **确定性**：同一段 `advance` 序列在两次测试进程中对「是否过期」的判定完全一致。
5. **生产拒绝注入**：`createControlPlane({ clock: attackerClock })` 在非测试标志下不使用 attackerClock；签发到消费的间隔按进程单调源计算。
6. **既有 invariants / adversarial** 里所有 `clock.advance(...)` 用例（含 120000ms、3600000ms）不得改期望，只改 clock 对象形状。
7. Ledger 哈希链不受影响：`expiresAtMonoNs` 如写入账本 payload，必须进入 `canonical`，不得只存在于内存却不入链。

## 不修的风险

- 任何能向控制面塞 clock 的调用方（测试缝泄漏到生产、或错误地把 fixture clock 配进部署）都能让过期 grant 复活。
- 墙上时钟回拨会破坏「单次、过期」这个 I12 承诺，而现有测试因为自己控制 clock，发现不了这件事。
- F-GAR-06 会继续以「生产会改单调源」的口头保证存在，但代码路径并未强制。
