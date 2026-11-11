"""四档基线对照仿真器（E-03）。

目的：把「无防护 / 幂等重试 / 加租约围栏 / 加证据与裁定」四档放在同一组故障注入下比较，
量化每一档降低了什么、代价是什么。

**这是仿真，不是系统级压测。** 它测的是「机制对故障的抑制效果」，不测真实吞吐与延迟。
真实系统级的十项验收在 `prototype/test_acceptance.py`，两者不互相替代。

故障模型（每条独立伯努利）：
  loss            消息丢失
  dup             重复投递
  crash_after     执行成功但落账前崩溃（外部副作用已发生、账本无记录）
  stale           旧围栏执行者迟到返回
  false_complete  执行者谎报完成（无证据）
  timeout         超时

档位策略：
  T0 无防护      —— 无幂等、无围栏、无裁定
  T1 +幂等重试   —— idempotency_key + redelivery
  T2 +租约围栏   —— lease/attempt/fence + 回收
  T3 +证据裁定   —— evidence grade + verdict（执行声明不等于通过）
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

TIERS = ("T0", "T1", "T2", "T3")

FAULTS = {
    "loss": 0.10,
    "dup": 0.10,
    "crash_after": 0.08,
    "stale": 0.08,
    "false_complete": 0.10,
    "timeout": 0.05,
}


@dataclass
class TaskResult:
    lost: bool = False
    duplicate_exec: bool = False
    wrong_accepted: bool = False      # 错误结果被当成成功收下
    stuck: bool = False               # 无终态
    recovered_ok: bool = False        # 正确收口
    ops: int = 0                      # 操作数（开销代理）


@dataclass
class TierStats:
    tier: str
    n: int = 0
    lost: int = 0
    duplicate_exec: int = 0
    wrong_accepted: int = 0
    stuck: int = 0
    recovered_ok: int = 0
    ops: int = 0
    per_task: list = field(default_factory=list)

    def as_row(self) -> dict:
        return {
            "tier": self.tier,
            "tasks": self.n,
            "lost": self.lost,
            "duplicate_exec": self.duplicate_exec,
            "wrong_accepted": self.wrong_accepted,
            "stuck": self.stuck,
            "recovered_ok": self.recovered_ok,
            "ops_per_task": round(self.ops / self.n, 3) if self.n else 0.0,
        }


def has_idempotency(tier: str) -> bool:
    return tier in ("T1", "T2", "T3")


def has_fencing(tier: str) -> bool:
    return tier in ("T2", "T3")


def has_verdict(tier: str) -> bool:
    return tier == "T3"


def simulate_task(tier: str, rng: random.Random) -> TaskResult:
    r = TaskResult()
    faults = {k: rng.random() < p for k, p in FAULTS.items()}
    r.ops = 1  # 提交

    # ---- 1. 消息丢失 ----
    if faults["loss"]:
        r.ops += 1
        if not has_idempotency(tier):
            r.lost = True
            return r
        # 幂等重投：同 key 重发，不算重复执行
        r.ops += 1

    # ---- 2. 重复投递 ----
    if faults["dup"]:
        r.ops += 1
        if has_idempotency(tier):
            r.ops += 1          # 去重查表
        else:
            r.duplicate_exec = True

    # ---- 3. 执行成功但落账前崩溃 ----
    if faults["crash_after"]:
        r.ops += 2              # intent + unknown
        if not has_idempotency(tier):
            r.stuck = True
            return r
        if has_fencing(tier):
            # 新尝试拿更大围栏；旧结果按 stale 处理，副作用未知需人工确认
            if has_verdict(tier):
                # 裁定要求证据，unknown 不能自动重试 → 拦下
                r.recovered_ok = True
                return r
            r.duplicate_exec = True   # 无裁定 → 自动重试，副作用可能翻倍
            return r
        r.duplicate_exec = True
        return r

    # ---- 4. 旧围栏执行者迟到 ----
    if faults["stale"]:
        r.ops += 1
        if has_fencing(tier):
            r.ops += 1          # 围栏校验，拒绝
        else:
            r.wrong_accepted = True
            return r

    # ---- 5. 谎报完成 ----
    if faults["false_complete"]:
        r.ops += 1
        if has_verdict(tier):
            # 无证据 → 不得判通过；按未核/不通过收口
            r.ops += 1
            r.recovered_ok = True
            return r
        r.wrong_accepted = True
        return r

    # ---- 6. 超时 ----
    if faults["timeout"]:
        r.ops += 1
        if has_fencing(tier):
            r.ops += 1          # 租约回收 + 重新派发
            r.recovered_ok = True
            return r
        r.stuck = True
        return r

    r.ops += 2                  # 正常路径：执行 + 收口
    r.recovered_ok = True
    return r


def run(tier: str, tasks: int = 2000, seed: int = 20261112) -> TierStats:
    rng = random.Random(seed)
    st = TierStats(tier=tier)
    for _ in range(tasks):
        r = simulate_task(tier, rng)
        st.n += 1
        st.lost += int(r.lost)
        st.duplicate_exec += int(r.duplicate_exec)
        st.wrong_accepted += int(r.wrong_accepted)
        st.stuck += int(r.stuck)
        st.recovered_ok += int(r.recovered_ok)
        st.ops += r.ops
        st.per_task.append(r)
    return st


def run_all(tasks: int = 2000, seed: int = 20261112) -> list[TierStats]:
    return [run(t, tasks, seed) for t in TIERS]
