# F-GAR-06：把 E6「命令真跑过」从 OUT OF SCOPE 升级为可验证

> 本文件是给 `governed-agent-runtime` 的**修复提案**，不是 UAI 实现。
> 针对修订：`3338902669-ops/governed-agent-runtime` @ `c754bbf`。
>
> **编号说明**：本文件名按 UAI 交接书 C-06 使用 `F-GAR-06`。它对应的是
> Agent Orchestra 的 **E6**（以及 UAI `NOT-GUARANTEED.md` 的 N4），**不是**
> GAR `KNOWN-FINDINGS.md` 里现有的 F-GAR-06（grant TTL 可注入时钟；见本目录
> `F-GAR-09-monotonic-clock.md`）。合入 GAR 时建议另开新 ID，避免与时钟项撞号。

## 问题复述

E1 证据只要求记录 `command` / `exit_code` / `revision`。这能证明「有人这样断言」，
不能证明该命令在诚实的执行环境里真的生成过进程、拿到过那个退出码。

现状：

- Orchestra 把 E6「命令真跑过」标为 **OUT OF SCOPE**。
- UAI `NOT-GUARANTEED.md` N4 明确不保证证据里的命令真的执行过。
- GAR `Ledger.recordEvidence` 在 `src/ledger.mjs` 接受调用方提交的
  `command` / `exitCode`，没有对照任何执行环境侧记录。
- `createGovernedRuntime().execute` 会调用密封的 `executor`，但执行收据
  只有 `startedAt` / `finishedAt` / `outcome`，没有进程树、退出码或观察者签名。

因此一个配合的（或不配合的）执行者可以编造 E1 字段而不留下可证伪的痕迹。

## 最小修复（针对 governed-agent-runtime）

引入**执行环境观察者**（runtime 进程内、executor 外包一层）写出一份不可由
被测 agent 直接改写的 `execution_attestation`，并规定 E6 的判定规则。
自报的 command/exit_code 仍然只构成 E1；只有与观察者记录对齐才升到 E6。

最小数据结构：

```json
{
  "attestationId": "att-…",
  "executionId": "exec-…",
  "grantId": "grant-…",
  "argv": ["python", "-m", "pytest", "-q"],
  "cwd": "/work",
  "pid": 4321,
  "exitCode": 0,
  "startedAtMonoNs": 1000000000,
  "endedAtMonoNs": 1500000000,
  "startedAtUtc": "2026-11-12T00:00:00.000Z",
  "endedAtUtc": "2026-11-12T00:00:00.500Z",
  "processTree": [
    {"pid": 4300, "ppid": 1, "argv": ["node", "runtime"]},
    {"pid": 4321, "ppid": 4300, "argv": ["python", "-m", "pytest", "-q"]}
  ],
  "stdoutSha256": "…64 hex…",
  "stderrSha256": "…64 hex…",
  "observer": "runtime-exec-probe",
  "observerKeyId": "observer-local-1",
  "signature": "…"
}
```

判定规则（全部满足才算 E6 可验证，否则仍是 E1 自报）：

1. 存在观察者签名的 attestation，且 `verifyObserverSignature` 通过。
2. `attestation.executionId` 绑定已消费的 grant 所产生的 execution。
3. 规范化后的 `argv` 与证据里的 `command` 一致（或为其精确前缀包装，例如 shell `-c`）。
4. `exitCode` 与证据里的 `exit_code` 一致。
5. `endedAtMonoNs >= startedAtMonoNs`，且时间窗落在 grant 的签发/过期（单调时钟）之内。
6. 进程树中存在一条记录：其 `argv` 对应被观察命令，且 `ppid` 属于 runtime 观察者进程。

缺观察者、签名失败、字段对不上，一律不得把 E6 标为成立。

```diff
--- a/src/ledger.mjs
+++ b/src/ledger.mjs
@@ -18,6 +18,7 @@ export class Ledger {
     this.grants = new Map();
     this.executions = new Map();
     this.evidence = new Map();
+    this.attestations = new Map();
     this.verifications = new Map();
     this.facts = new Map();
   }
@@
   recordExecution(input) {
     const execution = {
       executionId: newId('exec'),
       grantId: input.grantId,
       // ... existing fields ...
       startedAt: input.startedAt || this.#at(),
       finishedAt: this.#at(),
       outcome: input.outcome,
       produced: input.produced === undefined ? null : input.produced,
+      attestationId: input.attestationId === undefined ? null : input.attestationId,
     };
     // ... existing append ...
   }
+
+  /** Record an observer attestation. Agents cannot write this row. */
+  recordAttestation(input) {
+    if (!input || input.source !== 'runtime-exec-probe') {
+      throw new Refusal('ATTESTATION_FORBIDDEN', 'only the runtime observer may write attestations');
+    }
+    if (!input.signature || !input.observerKeyId) {
+      throw new Refusal('ATTESTATION_UNSIGNED', 'observer attestation must be signed');
+    }
+    if (input.endedAtMonoNs < input.startedAtMonoNs) {
+      throw new Refusal('ATTESTATION_NON_MONOTONIC', 'endedAtMonoNs precedes startedAtMonoNs');
+    }
+    const record = { ...input, attestationId: input.attestationId || newId('att') };
+    this.attestations.set(record.attestationId, record);
+    this.append('execution.attested', {
+      attestationId: record.attestationId,
+      executionId: record.executionId || null,
+      grantId: record.grantId,
+      exitCode: record.exitCode,
+      argv: record.argv,
+    });
+    return clone(record);
+  }
+
+  /**
+   * E6 holds only when evidence.command/exitCode match a verified observer record.
+   * Self-reported E1 fields alone never satisfy this.
+   */
+  e6Holds(evidenceId) {
+    const ev = this.getEvidence(evidenceId);
+    const execution = this.executions.get(ev.executionId);
+    if (!execution || !execution.attestationId) return { ok: false, reason: 'NO_ATTESTATION' };
+    const att = this.attestations.get(execution.attestationId);
+    if (!att) return { ok: false, reason: 'ATTESTATION_MISSING' };
+    if (!verifyObserverSignature(att)) return { ok: false, reason: 'BAD_SIGNATURE' };
+    if (normalizeArgv(att.argv) !== normalizeCommand(ev.command)) {
+      return { ok: false, reason: 'ARGV_MISMATCH' };
+    }
+    if (Number(att.exitCode) !== Number(ev.exitCode)) {
+      return { ok: false, reason: 'EXIT_MISMATCH' };
+    }
+    if (!processTreeContains(att.processTree, att.pid, att.argv)) {
+      return { ok: false, reason: 'PROCESS_TREE_MISMATCH' };
+    }
+    return { ok: true, reason: 'E6_VERIFIED', attestationId: att.attestationId };
+  }
```

`runtime.execute` 在调用用户 executor 前后由观察者采集 pid / argv / 退出码 / 单调时间戳，
再调用 `ledger.recordAttestation`（source 固定为 `runtime-exec-probe`）。观察者密钥只存在
于 runtime 进程，不暴露给 runner 表面。

## 验收测试

1. **诚实执行**：observer 记录的 argv 与 exitCode 与 E1 证据一致 → `e6Holds` 返回 `{ ok: true, reason: 'E6_VERIFIED' }`。
2. **只自报、无 attestation**：照旧可写入 E1 证据，但 `e6Holds` 返回 `NO_ATTESTATION`，不得把该项标为 E6。
3. **编造退出码**：证据 `exitCode=0`，观察者记录 `exitCode=1` → `EXIT_MISMATCH`。
4. **编造命令**：证据 command 与观察者 argv 不一致 → `ARGV_MISMATCH`。
5. **伪造观察者**：非 `runtime-exec-probe` 来源或错误签名 → `recordAttestation` 拒绝，或 `e6Holds` 为 `BAD_SIGNATURE`。
6. **时间倒退**：`endedAtMonoNs < startedAtMonoNs` → 写入被拒。
7. 既有 I12 / A17（execution 必须消费 gate 签发的 grant）不得被这次改动绕过。

## 不修的风险

- 外部评估可以继续把「有工程、证据却不可证伪」写进结论：E1 看起来像执行证明，实际只是自报。
- 任何「命令已跑过」的产品表述都与 N4 / E6 OUT OF SCOPE 冲突，容易被当成过声称。
- 配合的执行者可以用一份从未产生进程的 E1 记录通过核验，整个「结果可信」链条从第一环就空心。
