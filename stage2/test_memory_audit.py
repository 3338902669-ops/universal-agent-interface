"""P4 自检：审计（有结论未入图 / 降级写入 / 死信 / 覆盖率）。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE2))

import memory_audit as MA  # noqa: E402
import memory_outbox as MO  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def intent(i, **over):
    value = {
        "intent_id": "int-a%03d" % i,
        "kind": "verdict",
        "subject": "审计",
        "claim": "任务 %d 已裁定" % i,
        "evidence_level": "E1",
        "evidence": "cmd exit=0",
        "group_id": "team",
        "reference_time": "2026-11-20T01:59:51+08:00",
    }
    value.update(over)
    return value


def write_ledger(path, rows):
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def make_ledger(tasks):
    tmp = Path(tempfile.mkdtemp()) / "ledger.jsonl"
    rows = []
    for task_id, state in tasks:
        rows.append({"record": "TASK_STATE", "ts": "2026-11-20T01:00:00.000Z",
                     "task_id": task_id, "state": state})
    write_ledger(tmp, rows)
    return tmp


def test_counts():
    ob = MO.MemoryOutbox(Path(tempfile.mkdtemp()) / "outbox.db")
    ob.enqueue(intent(1, task_id="task-1"))
    ob.claim_due()
    ob.mark_delivered("int-a001", verified="verified", write_path="graphiti", degraded=False)
    ob.enqueue(intent(2, task_id="task-2"))
    ob.claim_due()
    ob.mark_delivered("int-a002", verified="unverified", write_path="neo4j", degraded=True)
    audit = MA.build_audit(ob, ledger_path=None)
    check("MA-1 统计总数 2", audit["memory"]["total"] == 2, str(audit["memory"]))
    check("MA-2 降级写入被列出", len(audit["degraded"]) == 1, str(len(audit["degraded"])))
    check("MA-3 降级条目带 intent_id",
          audit["degraded"][0]["intent_id"] == "int-a002", audit["degraded"][0]["intent_id"])


def test_unrecorded():
    ob = MO.MemoryOutbox(Path(tempfile.mkdtemp()) / "outbox.db")
    ob.enqueue(intent(3, task_id="task-3"))
    ob.claim_due()
    ob.mark_delivered("int-a003", verified="verified", write_path="graphiti", degraded=False)
    ledger = make_ledger([("task-3", "SUCCEEDED"), ("task-4", "TIMED_OUT")])
    audit = MA.build_audit(ob, ledger_path=ledger)
    ids = [row["task_id"] for row in audit["unrecorded"]]
    check("MA-4 有结论未入图被列出", ids == ["task-4"], str(ids))
    check("MA-5 已入图任务不算缺口", "task-3" not in ids, str(ids))
    check("MA-6 覆盖率 1/2", audit["coverage"] == {"with_memory": 1, "terminal": 2},
          str(audit["coverage"]))


def test_dead_letters():
    ob = MO.MemoryOutbox(Path(tempfile.mkdtemp()) / "outbox.db")
    ob.enqueue(intent(4))
    clock = time.time() + 60
    for _ in range(MO.MAX_ATTEMPTS):
        ob.claim_due(now=clock)
        ob.mark_failed("int-a004", "graph_down", now=clock)
        clock += 60
    audit = MA.build_audit(ob, ledger_path=None)
    check("MA-7 死信被列出", len(audit["dead_letters"]) == 1, str(len(audit["dead_letters"])))
    check("MA-8 死信带最后错误",
          "graph_down" in (audit["dead_letters"][0]["last_error"] or ""),
          str(audit["dead_letters"][0]["last_error"]))


def test_render():
    ob = MO.MemoryOutbox(Path(tempfile.mkdtemp()) / "outbox.db")
    ob.enqueue(intent(5, task_id="task-5"))
    ledger = make_ledger([("task-5", "FAILED")])
    text = MA.render_text(MA.build_audit(ob, ledger_path=ledger))
    check("MA-9 文本含未入图小节", "有结论未入图" in text, text[:60])
    check("MA-10 文本点名缺失任务", "task-5" in text, "")
    check("MA-11 文本含覆盖率", "覆盖" in text, "")
    check("MA-12 文本含死信小节", "死信" in text, "")


def test_empty():
    ob = MO.MemoryOutbox(Path(tempfile.mkdtemp()) / "outbox.db")
    audit = MA.build_audit(ob, ledger_path=None)
    check("MA-13 空库不炸", audit["memory"]["total"] == 0, str(audit["memory"]))
    check("MA-14 空库覆盖率 0/0", audit["coverage"] == {"with_memory": 0, "terminal": 0},
          str(audit["coverage"]))


def main() -> int:
    test_counts()
    test_unrecorded()
    test_dead_letters()
    test_render()
    test_empty()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
