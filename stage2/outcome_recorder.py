"""UAI-08：裁定结果写回团队记忆图谱（**已弃用，保留一版过渡**）。

弃用说明（2026-11-20）：生产写入点已迁移到 `stage2/memory_pipeline.py`
（outbox + Graphiti 主路 + Neo4j 兜底 + 回读校验）。本模块只剩
`graph_writes_enabled()` 仍被 hub 引用，其余函数仅作兼容与历史测试，
新代码不要调用。原因：它直写 Episodic 不产生实体/边/嵌入，语义检索查不到，
且绕过了 outbox 的重试与死信。

原始说明 —— UAI-08：裁定结果写回团队记忆图谱（生产调用方）。

背景：`team_memory.TeamMemoryBridge.record_outcome()` 之前**只有测试在调**，
真实任务跑完、裁定落下之后图谱什么也没学到——团队记忆是只读的，等于没有记忆。

本模块是 hub → 图谱的**唯一写入点**，三条硬约束：

  1. **幂等**：同一 `(task_id, verdict)` 只写一份，靠图上的 `uai_key` MERGE，
     不看进程内存——hub 重启后不会重复写，图谱里也不会出现两份同任务裁定。
  2. **不写凭据**：任务文本里出现 api_key / password / secret / token= 之类字样
     就整条跳过并给出原因。宁可少一条记忆，也不能把密钥写进图。
  3. **fail-open**：图谱挂了或慢了不阻塞裁定。裁定是权威，图谱是派生数据；
     `record_verdict_outcome()` 永不抛异常，只返回结果。

写入结果（写出 / 跳过 / 失败 + 原因 + 键）会返回给调用方；hub 把它放进裁定响应，
失败时另在账本留一条 `MEMORY_OUTCOME`，让「记忆写丢」这件事可查而不是静默。
"""

from __future__ import annotations

import os

from team_memory import TeamMemoryBridge, _looks_like_credential

DEFAULT_SOURCE = "uai.hub.on_verdict"

# 开关：`UAI_GRAPH_OUTCOMES=0` 时完全不写图谱。
# 生产默认开启；单元测试必须关掉，否则测试的假任务会往真图谱写垃圾
# （2026-11-16 已经踩过一次：14 条测试污染 Episodic 之后人工清理）。
def graph_writes_enabled() -> bool:
    return os.environ.get("UAI_GRAPH_OUTCOMES", "1").strip().lower() \
        not in ("0", "false", "off", "no")

# 便于运维观察；不是权威计数（重启归零），权威计数在账本里
STATS = {"written": 0, "created": 0, "deduped": 0, "skipped": 0, "failed": 0}

_BRIDGE = None


def _default_bridge() -> TeamMemoryBridge:
    global _BRIDGE
    if _BRIDGE is None:
        _BRIDGE = TeamMemoryBridge()
    return _BRIDGE


def outcome_key(task_id: str, verdict: str) -> str:
    """幂等键：同一任务的同一裁定只有一条记忆。"""
    return "%s|%s" % (task_id, verdict)


def record_verdict_outcome(*, task_id: str, verdict: str, agent_id: str | None = None,
                           description: str | None = None, success: bool = True,
                           source: str = DEFAULT_SOURCE,
                           bridge: TeamMemoryBridge | None = None) -> dict:
    """把一次裁定写回图谱。**永不抛异常**（fail-open）。"""
    if not graph_writes_enabled():
        STATS["skipped"] += 1
        return {"written": False, "reason": "disabled_by_env"}
    if not task_id or not str(task_id).strip():
        STATS["skipped"] += 1
        return {"written": False, "reason": "missing_task_id"}
    if not verdict or not str(verdict).strip():
        STATS["skipped"] += 1
        return {"written": False, "reason": "missing_verdict"}

    text = description or ""
    if _looks_like_credential(text):
        # 不截图、不改写、不部分写入：整条跳过，留下原因
        STATS["skipped"] += 1
        return {"written": False, "reason": "credential_like_text",
                "key": outcome_key(task_id, verdict)}

    key = outcome_key(task_id, verdict)
    b = bridge if bridge is not None else _default_bridge()
    try:
        res = b.record_outcome(
            agent_id or "unknown",
            text or ("task %s" % task_id),
            bool(success),
            verdict,
            source=source,
            key=key,
        )
    except Exception as exc:                       # fail-open：图谱不能挡住裁定
        STATS["failed"] += 1
        return {"written": False, "reason": "graph_error:%s" % str(exc)[:120], "key": key}

    if not isinstance(res, dict) or not res.get("ok"):
        STATS["failed"] += 1
        return {"written": False,
                "reason": "graph_rejected:%s" % str((res or {}).get("reason"))[:120],
                "key": key}

    created = bool(res.get("created"))
    STATS["written"] += 1
    STATS["created" if created else "deduped"] += 1
    return {"written": True, "created": created, "key": key,
            "seen": res.get("seen"), "deduped": not created}


def reset_stats() -> None:
    for k in STATS:
        STATS[k] = 0
