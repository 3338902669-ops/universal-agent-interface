"""UAI 记忆管道：把 intent 落进 outbox，再驱动 writer 投递。

这是「一处写入、处处可用」的对外入口：CLI、hub、Future adapter 都走这里，
不各自实现 HTTP/Neo4j 细节。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from memory_outbox import MemoryOutbox
from memory_writer import drain

DEFAULT_LEDGER = os.environ.get(
    "UAI_LEDGER_PATH",
    "C:/Users/33389/.shared/comms-v2/cutover-base-20261109/data/ledger.jsonl",
)


def default_agent() -> str:
    return os.environ.get("UAI_AGENT_ID", "dsh")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_intent_id() -> str:
    import uuid

    return str(uuid.uuid4())


def build_intent(
    *,
    kind: str,
    claim: str,
    subject: str,
    evidence_level: str = "E3",
    evidence: str = "",
    task_id: str | None = None,
    group_id: str = "team",
    agent: str | None = None,
    session: str = "",
    intent_id: str | None = None,
    reference_time: str | None = None,
    refs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "intent_id": intent_id or new_intent_id(),
        "kind": kind,
        "subject": subject,
        "claim": claim,
        "evidence_level": evidence_level,
        "evidence": evidence,
        "source_agent": agent or default_agent(),
        "source_session": session,
        "group_id": group_id,
        "reference_time": reference_time or _now_iso(),
        "sensitivity": "internal",
        "task_id": task_id,
        "refs": refs or {},
    }


def remember(
    intent: dict[str, Any],
    *,
    db_path: str | None = None,
    flush: bool = True,
    limit: int = 10,
    **writer_kwargs: Any,
) -> dict[str, Any]:
    """入队并（默认）立即投递一次。永不抛异常地丢数据：入队失败会返回原因。"""
    outbox = MemoryOutbox(db_path)
    queued = outbox.enqueue(intent)
    if not queued["enqueued"]:
        return {
            "enqueued": False,
            "duplicate": bool(queued.get("duplicate")),
            "written": False,
            "reason": queued.get("reason"),
        }
    if not flush:
        return {"enqueued": True, "written": False, "status": "pending",
                "intent_id": intent["intent_id"], "reason": "deferred"}

    batch = drain(outbox, limit=limit, **writer_kwargs)
    row = outbox.get(intent["intent_id"]) or {}
    return {
        "enqueued": True,
        "intent_id": intent["intent_id"],
        "written": row.get("status") == "delivered",
        "status": row.get("status"),
        "path": row.get("write_path"),
        "degraded": bool(row.get("degraded")),
        "verified": row.get("verified"),
        "reason": row.get("last_error"),
        "drain": batch,
    }


def record_verdict(
    *,
    task_id: str,
    verdict: str,
    agent_id: str,
    description: str,
    db_path: str | None = None,
    flush: bool = True,
    **writer_kwargs: Any,
) -> dict[str, Any]:
    intent = build_intent(
        kind="verdict",
        subject="任务裁定 %s" % task_id,
        claim="任务 %s 由 %s 执行，裁定为「%s」" % (task_id, agent_id, verdict),
        evidence_level="E1",
        evidence="hub.on_verdict verdict=%s" % verdict,
        task_id=task_id,
        agent=agent_id,
        intent_id="verdict:%s:%s" % (task_id, verdict),
        refs={"verdict": verdict, "success": verdict in ("通过", "带注通过")},
    )
    result = remember(intent, db_path=db_path, flush=flush, **writer_kwargs)
    result["key"] = "%s|%s" % (task_id, verdict)
    return result


def record_terminal(
    *,
    task_id: str,
    state: str,
    agent_id: str,
    description: str,
    reason: str = "",
    db_path: str | None = None,
    **writer_kwargs: Any,
) -> dict[str, Any]:
    kind = "blockage" if state == "CANCELLED" else "failure"
    intent = build_intent(
        kind=kind,
        subject="任务%s %s" % ("取消" if state == "CANCELLED" else "失败", task_id),
        claim="任务 %s 在 %s 上进入 %s，原因：%s"
        % (task_id, agent_id, state, reason or "未提供"),
        evidence_level="E1",
        evidence="hub state=%s" % state,
        task_id=task_id,
        agent=agent_id,
        intent_id="terminal:%s:%s:%s" % (task_id, state, (reason or "none")[:40]),
        refs={"state": state, "reason": reason[:200]},
    )
    return remember(intent, db_path=db_path, flush=True, **writer_kwargs)
