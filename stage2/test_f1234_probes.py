"""F1–F4 隔离反例：天枢静态审读 + agent-15 独立复核的缺陷，逐个实测定性。

第一轮（天枢回执 RE-tianshu-event-bridge-verify-20261112.md）：
  F1 缺结果指针可能永久阻塞该任务；
  F2 重复序号且缺状态兜底可能吞终态；
  F3 部分追加后使用同一列表重试可能重复；
  F4 裁定先于状态时独立时序可能丢失。

第二轮（agent-15 复核回执 RE-agent15-eventbridge-reverify-20261112.md，FAIL）：
  F1b' force_pending_flush 只调 convert，静默丢弃挂起的 VERDICT/TASK_STATE；
  F3'  重启后 _written 不恢复，直接重试旧列表 → outbox 2 行变 4 行；
  F4'  fallback 路径 final=True + completed final=True = 两条 final；
  0'   探针退出码恒 0，CONFIRMED 无法被 CI 捕获。

以上全部纳入本探针。判定口径：PASS / CONFIRMED；main 返回非 0 当存在 CONFIRMED。
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
    return kind == "PASS"


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
    all_ok = True

    # ---------- F1：result_ready 永远等不到指针 → 任务永久阻塞？ ----------
    tmp = Path(tempfile.mkdtemp(prefix="f1-"))
    led = make(tmp)
    append(led, {"record": "TASK_EVENT", "task_id": "f1a", "seq": 1, "kind": "result_ready"},
           {"record": "TASK_EVENT", "task_id": "f1b", "seq": 1, "kind": "accepted"})
    b = EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True)
    evs = flush(b)
    kinds = [e["kind"] for e in evs]
    all_ok &= report("PASS" if kinds == ["task"] and not any(e.get("task_id") == "f1a" for e in evs)
                     else "CONFIRMED",
                     "F1 无指针任务不阻塞他任务", "kinds=%s" % kinds)
    # F1b（含 agent-15 反例）：默认扣压；逃生口必须按类型路由整条挂起链——
    # VERDICT 走 synth、TASK_STATE 先 absorb 指针，completed 终态带裁定而非「待核验」。
    append(led, {"record": "VERDICT", "task_id": "f1a", "verdict": "通过"},
           {"record": "TASK_STATE", "task_id": "f1a", "state": "SUCCEEDED"},
           {"record": "TASK_EVENT", "task_id": "f1a", "seq": 2, "kind": "completed"})
    evs2 = flush(b)
    finals_default = [e for e in evs2 if e.get("final")]
    flushed = b.force_pending_flush()
    b.write(flushed)
    lines = outbox_lines(tmp / "data" / "o.jsonl")
    finals_after = [e for e in lines if e.get("final") and e.get("task_id") == "f1a"]
    texts_after = [(e.get("status") or {}).get("message", {}).get("parts", [{}])[0].get("text")
                   for e in lines if e.get("task_id") == "f1a" and e.get("status")]
    # 权威口径：completed 携带裁定发 1 条 final（text=通过）；
    # VERDICT 同值 update 被 _verdict_emitted 拦下不算缺陷（durability 语义）。
    ok = (not finals_default and len(finals_after) == 1
          and texts_after == ["通过"])
    all_ok &= report("PASS" if ok else "CONFIRMED",
                     "F1b 逃生口发布整条挂起链（终态带裁定，非待核验）",
                     "default_finals=%d after=%d verdict_texts=%s"
                     % (len(finals_default), len(finals_after), texts_after))
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
    all_ok &= report("PASS" if errs and errs_persisted and not evs3 else "CONFIRMED",
                     "F2 重复 seq 终态 → bridge_error 审计（不静默吞，重启不重复）",
                     "errs=%d persisted=%d after_restart=%d"
                     % (len(errs), len(errs_persisted), len(evs3)))
    b.close()

    # ---------- F3：write 重试幂等（同进程 + 跨重启，agent-15 反例） ----------
    # agent-15 实测：重启后 _written 不恢复，直接重试旧列表 → outbox 2 行变 4 行。
    # 修复：_load_emitted 与 _seen 同源恢复 _written。
    tmp = Path(tempfile.mkdtemp(prefix="f3-"))
    led = make(tmp)
    append(led, {"record": "TASK_EVENT", "task_id": "f3", "seq": 1, "kind": "accepted"})
    b = EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True)
    evs = flush(b)
    b.close()
    b2 = EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True)
    b2.write(evs)               # 跨重启直接重试同一列表
    lines = outbox_lines(tmp / "data" / "o.jsonl")
    rc = b2.write(evs)          # 同进程再试一次
    lines2 = outbox_lines(tmp / "data" / "o.jsonl")
    all_ok &= report("PASS" if len(lines) == len(evs) and len(lines2) == len(evs) and rc == 0
                     else "CONFIRMED",
                     "F3 跨重启+同进程重试 write 均幂等",
                     "after_restart=%d after_retry=%d expected=%d"
                     % (len(lines), len(lines2), len(evs)))
    b2.close()

    # ---------- F4：VERDICT 先于 TASK_STATE → 独立时序与单终态 ----------
    # agent-15 反例：fallback 路径 final=True + completed final=True = 两条 final。
    # 修复：used_fallback 时裁定 final=False，completed 携带裁定单独发 final。
    tmp = Path(tempfile.mkdtemp(prefix="f4-"))
    led = make(tmp)
    append(led, {"record": "VERDICT", "task_id": "f4", "verdict": "通过", "ts": "T1"},
           {"record": "TASK_STATE", "task_id": "f4", "state": "SUCCEEDED", "ts": "T2"},
           {"record": "TASK_EVENT", "task_id": "f4", "seq": 1, "kind": "completed", "ts": "T3"})
    b = EventBridge(tmp, tmp / "data" / "o.jsonl", experimental_test_only=True)
    evs = flush(b)
    verdict_updates = [e for e in evs if e.get("uai/source") == "VERDICT"]
    finals = [e for e in evs if e.get("final")]
    texts = [(e.get("status") or {}).get("message", {}).get("parts", [{}])[0].get("text") for e in finals]
    ok = (len(verdict_updates) == 1 and verdict_updates[0].get("final") is False
          and len(finals) == 1 and "通过" in (texts or []))
    all_ok &= report("PASS" if ok else "CONFIRMED",
                     "F4 fallback 裁定 final=False + completed 单终态带裁定",
                     "updates=%d update_final=%s finals=%s"
                     % (len(verdict_updates),
                        verdict_updates[0].get("final") if verdict_updates else None,
                        texts))
    b.close()

    print("\nSUMMARY: PASS=%d CONFIRMED=%d NOTE=%d"
          % (len(PASSES), len(CONFIRMED), len(NOTES)))
    print("CONFIRMED: %s" % "; ".join(CONFIRMED))
    # agent-15 指出：退出码恒 0 使 CONFIRMED 无法被 CI 捕获——已改为非 0。
    return 0 if all_ok and not CONFIRMED else 1


if __name__ == "__main__":
    sys.exit(main())
