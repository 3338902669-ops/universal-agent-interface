"""P2 自检：SQLite Durable Outbox 状态机（幂等、退避、死信、崩溃恢复）。"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE2))

import memory_outbox as MO  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def make():
    db = Path(tempfile.mkdtemp()) / "outbox.db"
    return MO.MemoryOutbox(db)


def intent(i, **over):
    value = {
        "intent_id": "int-%03d" % i,
        "kind": "decision",
        "subject": "主题 %d" % i,
        "claim": "事实陈述 %d" % i,
        "evidence_level": "E1",
        "evidence": "cmd exit=0",
        "group_id": "team",
        "reference_time": "2026-11-20T01:59:51+08:00",
    }
    value.update(over)
    return value


def test_enqueue():
    ob = make()
    first = ob.enqueue(intent(1))
    check("MO-1 首次入队 enqueued=True", first["enqueued"] is True and first["duplicate"] is False,
          str(first))
    dup = ob.enqueue(intent(1))
    check("MO-2 重复 intent_id 去重", dup["enqueued"] is False and dup["duplicate"] is True, str(dup))
    check("MO-3 重复不新增行", ob.stats()["total"] == 1, str(ob.stats()))


def test_claim_and_deliver():
    ob = make()
    ob.enqueue(intent(2))
    due = ob.claim_due(limit=10)
    check("MO-4 claim_due 取到待发", len(due) == 1 and due[0]["intent_id"] == "int-002",
          str([d["intent_id"] for d in due]))
    check("MO-5 claim 后状态 inflight", ob.get("int-002")["status"] == "inflight",
          ob.get("int-002")["status"])
    check("MO-6 已 inflight 不再被重复取",
          ob.claim_due(limit=10) == [], "should be empty")
    ob.mark_delivered("int-002", verified="verified", write_path="graphiti", degraded=False)
    row = ob.get("int-002")
    check("MO-7 mark_delivered 落状态", row["status"] == "delivered", row["status"])
    check("MO-8 记录 write_path 与 verified",
          row["write_path"] == "graphiti" and row["verified"] == "verified",
          "%s/%s" % (row["write_path"], row["verified"]))


def test_retry_and_dead():
    ob = make()
    ob.enqueue(intent(3))
    ob.claim_due()
    now = time.time()
    ob.mark_failed("int-003", "graph_down", now=now)
    row = ob.get("int-003")
    check("MO-9 首次失败进入 retry", row["status"] == "retry", row["status"])
    check("MO-10 退避后 next_attempt_at 在未来",
          row["next_attempt_at"] >= now + 1, str(row["next_attempt_at"] - now))
    check("MO-11 失败次数 = 1", row["attempts"] == 1, str(row["attempts"]))
    # 立即再 claim 不应取到（还没到点）
    check("MO-12 未到点不被 claim", ob.claim_due(now=now) == [], "should be empty")

    # 退避是递增的：每轮把时钟往前推，才是在测「claim→fail 循环直到死信」
    clock = now + 60
    for _ in range(MO.MAX_ATTEMPTS - 1):
        ob.claim_due(now=clock)
        ob.mark_failed("int-003", "graph_down", now=clock)
        clock += 60
    row = ob.get("int-003")
    check("MO-13 达到上限进入 dead_letter", row["status"] == "dead_letter", row["status"])
    check("MO-14 死信记录最后错误", "graph_down" in (row["last_error"] or ""), str(row["last_error"]))


def test_stale_recovery():
    ob = make()
    ob.enqueue(intent(4))
    ob.claim_due()
    recovered = ob.recover_stale(older_than_s=0, now=time.time() + 1)
    check("MO-15 崩溃遗留 inflight 可回收", recovered == 1, str(recovered))
    check("MO-16 回收后回到 retry", ob.get("int-004")["status"] == "retry",
          ob.get("int-004")["status"])


def test_replay_and_stats():
    ob = make()
    ob.enqueue(intent(5))
    clock = time.time() + 60
    for _ in range(MO.MAX_ATTEMPTS):
        ob.claim_due(now=clock)
        ob.mark_failed("int-005", "boom", now=clock)
        clock += 60
    check("MO-17 死信可枚举", len(ob.dead_letters()) == 1, str(len(ob.dead_letters())))
    check("MO-18 replay 成功", ob.replay("int-005") is True, "")
    check("MO-19 replay 后回到 pending", ob.get("int-005")["status"] == "pending",
          ob.get("int-005")["status"])
    check("MO-20 replay 清零 attempts", ob.get("int-005")["attempts"] == 0,
          str(ob.get("int-005")["attempts"]))
    s = ob.stats()
    check("MO-21 stats 有 total/pending", s["total"] == 1 and s["pending"] == 1, str(s))


def test_task_id_and_payload():
    ob = make()
    ob.enqueue(intent(6, task_id="task-abc"))
    row = ob.get("int-006")
    check("MO-22 task_id 可索引审计", row["task_id"] == "task-abc", str(row.get("task_id")))
    check("MO-23 payload 原样保留", "int-006" in row["payload"], row["payload"][:40])


def main() -> int:
    test_enqueue()
    test_claim_and_deliver()
    test_retry_and_dead()
    test_stale_recovery()
    test_replay_and_stats()
    test_task_id_and_payload()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
