"""F1–F4 隔离反例：天枢静态审读提出的四个风险，逐个实测定性。

回执原文（RE-tianshu-event-bridge-verify-20261112.md）：
  F1 缺结果指针可能永久阻塞该任务；
  F2 重复序号且缺状态兜底可能吞终态；
  F3 部分追加后使用同一列表重试可能重复；
  F4 裁定先于状态时独立时序可能丢失。

判定口径：每个反例给出 PASS（无此缺陷）/ CONFIRMED（缺陷成立，附最小修复）/ QUALIFIED。
不编造：bridge 行为以实跑输出为准。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from event_bridge import EventBridge  # noqa: E402

PASSES, CONFIRMED, NOTES = [], [], []


def report(kind, name, detail=""):
    {"PASS": PASSES, "CONFIRMED": CONFIRMED, "NOTE": NOTES}[kind].append(name)
    print("%-10s %-56s %s" % (kind, name, detail))


def make(tmp: Path):
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    ledger = tmp / "data" / "ledger.jsonl"
    ledger.touch()
    return ledger


def append(ledger: Path, *records):
    with ledger.open("a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def flush(b: EventBridge):
    evs = b.drain()
    b.write(evs)
    return evs


def restart(b: EventBridge):
    b.close()
    return EventBridge(b.ledger.parent.parent, b.out, experimental_test_only=True)


def outbox_lines(out: Path):
    if not out.exists():
        return []
    return [json.loads(x) for x in out.read_text(encoding="utf-8").splitlines() if x.strip()]


def main() -> int:
    # ---------- F1：result_ready 永远等不到指针 → 任务永久阻塞？ ----------
    # 场景：账本里 result_ready 之后再无 TASK_STATE（指针永远不来）。
    # 期望：不吞事件也不崩；桥对该任务保持 pending，但**不影响其他任务**。
    tmp = Path(tempfile.mkdtemp(prefix="f1-"))
    led = make(tmp)
    append(led, {"record": "TASK_EVENT", "task_id": "f1a", "seq": 1, "kind": "result_ready"},
           {"record": "TASK_EVENT", "task_id": "f1b", "seq": 1, "kind": "accepted"})
    b = EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True)
    evs = flush(b)
    kinds = [e["kind"] for e in evs]
    report("PASS" if kinds == ["task"] and not any(e.get("task_id") == "f1a" for e in evs)
           else "CONFIRMED",
           "F1 无指针任务不阻塞他任务", "kinds=%s" % kinds)
    # 终态来临但指针永远缺席：completed 会被扣压（默认语义=等指针），
    # 但 force_pending_flush() 逃生口必须能把它发布出来（F1b 修复）。
    append(led, {"record": "TASK_EVENT", "task_id": "f1a", "seq": 2, "kind": "completed"})
    evs2 = flush(b)
    finals_default = [e for e in evs2 if e.get("final")]
    flushed = b.force_pending_flush()
    b.write(flushed)
    lines = outbox_lines(tmp / "data" / "o.jsonl")
    finals_after = [e for e in lines if e.get("final") and e.get("task_id") == "f1a"]
    report("PASS" if not finals_default and len(finals_after) == 1 else "CONFIRMED",
           "F1b 默认扣压 + force_pending_flush 逃生口发布终态",
           "default_finals=%d after_flush=%d" % (len(finals_default), len(finals_after)))
    b.close()

    # ---------- F2：重复 seq 且缺 TASK_STATE 兜底 → 吞终态？ ----------
    tmp = Path(tempfile.mkdtemp(prefix="f2-"))
    led = make(tmp)
    append(led, {"record": "TASK_EVENT", "task_id": "f2", "seq": 1, "kind": "accepted"})
    evs = flush(b := EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True))
    b = restart(b)
    # 重复 seq=1 的 completed（账本错误），且没有 TASK_STATE 兜底。
    # 修复后语义（契约 §11）：不静默吞——发 bridge_error 审计记录；
    # 终态本身仍缺失（账本错误不该凭空造终态），但不再"无声消失"。
    append(led, {"record": "TASK_EVENT", "task_id": "f2", "seq": 1, "kind": "completed"})
    evs2 = flush(b)
    errs = [e for e in evs2 if e.get("kind") == "bridge_error" and "dup_terminal" in (e.get("uai/synth") or "")]
    lines = outbox_lines(tmp / "data" / "o.jsonl")
    errs_persisted = [e for e in lines if "dup_terminal" in (e.get("uai/synth") or "")]
    # 重启后不得重复
    b = restart(b)
    evs3 = flush(b)
    report("PASS" if errs and errs_persisted and not evs3 else "CONFIRMED",
           "F2 重复 seq 终态 → bridge_error 审计（不静默吞，重启不重复）",
           "errs=%d persisted=%d after_restart=%d"
           % (len(errs), len(errs_persisted), len(evs3)))
    b.close()

    # ---------- F3：write 失败后同批列表重试 → 重复？ ----------
    # durability 套件已覆盖 write 失败重试（same process & after restart）。
    # 这里实测「同一列表二次 write」是否产生重复行。
    tmp = Path(tempfile.mkdtemp(prefix="f3-"))
    led = make(tmp)
    append(led, {"record": "TASK_EVENT", "task_id": "f3", "seq": 1, "kind": "accepted"})
    b = EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True)
    evs = flush(b)
    b.write(evs)  # 同一列表故意再写一次
    lines = outbox_lines(tmp / "data" / "o.jsonl")
    report("CONFIRMED" if len(lines) > len(evs) else "PASS",
           "F3 同一列表二次 write 重复",
           "events=%d outbox_lines=%d" % (len(evs), len(lines)))
    b.close()

    # ---------- F4：VERDICT 先于 TASK_STATE → 独立时序丢失？ ----------
    # F4 修复后语义：先到的裁定按自己的时序发布（uai/source=VERDICT, final=False），
    # completed 随后携带裁定发出终态——两条都在，独立时序保住。
    append(led, {"record": "VERDICT", "task_id": "f4", "verdict": "通过", "ts": "T1"},
           {"record": "TASK_STATE", "task_id": "f4", "state": "SUCCEEDED", "ts": "T2"},
           {"record": "TASK_EVENT", "task_id": "f4", "seq": 1, "kind": "completed", "ts": "T3"})
    b = EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True)
    evs = flush(b)
    verdict_updates = [e for e in evs if e.get("uai/source") == "VERDICT"]
    finals = [e for e in evs if e.get("final")]
    texts = [(e.get("status") or {}).get("message", {}).get("parts", [{}])[0].get("text") for e in finals]
    ok = len(verdict_updates) == 1 and "通过" in (texts or [])
    report("PASS" if ok else "CONFIRMED",
           "F4 先到的裁定按独立时序发布（1 update + 1 final 带裁定）",
           "verdict_updates=%d finals=%s" % (len(verdict_updates), texts))
    b.close()

    print("\nSUMMARY: PASS=%d CONFIRMED=%d NOTE=%d"
          % (len(PASSES), len(CONFIRMED), len(NOTES)))
    print("CONFIRMED: %s" % "; ".join(CONFIRMED))
    return 0


if __name__ == "__main__":
    sys.exit(main())
