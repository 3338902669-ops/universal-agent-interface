"""EventBridge v2 验收测试（规格 §7 矩阵 + 三轮复核反例回归）。

跑法：python test_event_bridge_v2.py ；退出码 0 = 全绿。
本文件是规格 EVENTBRIDGE-V2-SPEC.md §7 的机器执行。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from event_bridge_v2 import EventBridge  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def expect_error(name, fn, needle):
    try:
        fn()
    except RuntimeError as e:
        check(name, needle in str(e), str(e)[:70])
        return
    check(name, False, "未抛错")


class Lab:
    def __init__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        (self.base / "data").mkdir()
        self.ledger = self.base / "data" / "ledger.jsonl"
        self.ledger.touch()
        self.out = self.base / "data" / "o.jsonl"
        self.bridge = self._new()

    def _new(self):
        return EventBridge(self.base, self.out, experimental_test_only=True)

    def append(self, *records):
        with self.ledger.open("a", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

    def drain(self):
        evs = self.bridge.drain()
        self.bridge.write(evs)
        return evs

    def flush(self):
        evs = self.bridge.force_pending_flush()
        self.bridge.write(evs)
        return evs

    def restart(self):
        self.bridge.close()
        self.bridge = self._new()

    def lines(self):
        if not self.out.exists():
            return []
        return [json.loads(x) for x in self.out.read_text(encoding="utf-8").splitlines() if x.strip()]

    def close(self):
        self.bridge.close()
        try:
            self.tmp.cleanup()
        except PermissionError:
            # Windows 上 ledger 句柄释放有延迟；临时目录由 OS 回收，不影响断言
            pass


def texts(evs):
    return [(e.get("status") or {}).get("message", {}).get("parts", [{}])[0].get("text")
            for e in evs if e.get("status")]


def finals_in(lines, tid=None):
    return [e for e in lines if e.get("final") and (tid is None or e.get("task_id") == tid)]


def main() -> int:
    # ===== 三轮复核反例回归（r1/r2/r3 全部） =====
    # --- r1-F1b/r2-F1b：挂起链经 flush 发布，终态带裁定 ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "f1a", "seq": 1, "kind": "result_ready"},
               {"record": "TASK_EVENT", "task_id": "f1b", "seq": 1, "kind": "accepted"},
               {"record": "VERDICT", "task_id": "f1a", "verdict": "通过"},
               {"record": "TASK_STATE", "task_id": "f1a", "state": "SUCCEEDED"},
               {"record": "TASK_EVENT", "task_id": "f1a", "seq": 2, "kind": "completed"})
    d1 = lab.drain()
    check("r1-F1b 默认挂起不发布", not any(e.get("task_id") == "f1a" for e in d1), str(len(d1)))
    f1 = lab.flush()
    check("r1-F1b flush 发布终态带裁定「通过」",
          "通过" in texts(f1) and any(e.get("final") for e in f1), str(texts(f1)))
    lab.close()

    # --- r1-F2：重复 seq 终态 → 审计，重启不重复 ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "f2", "seq": 1, "kind": "accepted"})
    lab.drain()
    lab.restart()
    lab.append({"record": "TASK_EVENT", "task_id": "f2", "seq": 1, "kind": "completed"})
    d = lab.drain()
    errs = [e for e in d if e.get("kind") == "bridge_error" and "dup" in (e.get("uai/synth") or "")]
    n1 = len(lab.lines())
    lab.restart()
    d2 = lab.drain()
    check("r1-F2 dup 终态发审计且重启不重复",
          bool(errs) and n1 == len(lab.lines()) and not d2,
          "errs=%d restart=%d" % (len(errs), len(d2)))
    lab.close()

    # --- r1-F3：跨重启直接重试 write 幂等 ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "f3", "seq": 1, "kind": "accepted"})
    b = lab.bridge
    evs = b.drain()
    b.write(evs)
    b.close()
    b2 = lab._new()
    b2.write(evs)
    b2.write(evs)
    check("r1-F3 跨重启+同进程重试 write 幂等", len(lab.lines()) == len(evs),
          "%d vs %d" % (len(lab.lines()), len(evs)))
    lab.close()

    # --- r1/r2-F4：fallback 裁定 final=False + completed 单终态 ---
    lab = Lab()
    lab.append({"record": "VERDICT", "task_id": "f4", "verdict": "通过", "ts": "T1"},
               {"record": "TASK_STATE", "task_id": "f4", "state": "SUCCEEDED", "ts": "T2"},
               {"record": "TASK_EVENT", "task_id": "f4", "seq": 1, "kind": "completed", "ts": "T3"})
    d = lab.drain()
    vu = [e for e in d if e.get("uai/source") == "VERDICT"]
    fin = [e for e in d if e.get("final")]
    check("r2-F4 fallback update final=False + completed 唯一 final 带裁定",
          len(vu) == 1 and vu[0].get("final") is False
          and len(fin) == 1 and texts(fin) == ["通过"],
          "vu=%s finals=%s" % (vu[0].get("final") if vu else None, texts(fin)))
    lab.close()

    # --- r2-P1：交错两任务 flush 不丢 progress ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "a", "seq": 1, "kind": "result_ready"},
               {"record": "TASK_EVENT", "task_id": "b", "seq": 1, "kind": "result_ready"},
               {"record": "TASK_EVENT", "task_id": "a", "seq": 2, "kind": "progress"},
               {"record": "TASK_EVENT", "task_id": "b", "seq": 2, "kind": "progress"},
               {"record": "TASK_EVENT", "task_id": "a", "seq": 3, "kind": "failed"},
               {"record": "TASK_EVENT", "task_id": "b", "seq": 3, "kind": "cancelled"})
    lab.drain()
    f = lab.flush()
    allk = [e.get("uai/kind") for e in f] + [e.get("uai/seq") for e in lab.lines()]
    # progress 以 status_update(final=False, seq=2) 形态保留；终态 seq=3 final=True。
    seqs = [e.get("uai/seq") for e in lab.lines()]
    check("r2-P1 交错任务 flush 后 progress 不丢、终态齐全",
          2 in seqs and 3 in seqs
          and sum(1 for e in lab.lines() if e.get("final")) == 2,
          "seqs=%s" % seqs)
    lab.close()

    # --- r2-P2：多裁定 flush 按账本序，completed 带最后裁定 ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "c", "seq": 1, "kind": "result_ready"},
               {"record": "VERDICT", "task_id": "c", "verdict": "通过"},
               {"record": "VERDICT", "task_id": "c", "verdict": "不通过"},
               {"record": "TASK_STATE", "task_id": "c", "state": "SUCCEEDED"},
               {"record": "TASK_EVENT", "task_id": "c", "seq": 2, "kind": "completed"})
    lab.drain()
    f = lab.flush()
    check("r2-P2 flush 裁定按账本序（通过→不通过）、completed 带最后裁定",
          texts(f) == ["通过", "不通过", "不通过"], str(texts(f)))
    lab.close()

    # --- r3-P1：force 后无指针 result_ready 不丢 artifact（指针迟到） ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "r3a", "seq": 1, "kind": "result_ready"},
               {"record": "TASK_EVENT", "task_id": "r3a", "seq": 2, "kind": "completed"})
    lab.drain()
    lab.flush()
    lab.append({"record": "TASK_STATE", "task_id": "r3a", "state": "SUCCEEDED",
                "result_pointer": {"uri": "file:///result"}})
    d = lab.drain()
    arts = [e for e in d if e.get("kind") == "artifact_update"]
    check("r3-P1 指针迟到后 artifact 按原 seq 补发（不丢）",
          len(arts) == 1 and arts[0].get("uai/seq") == 1,
          "%d arts seq=%s" % (len(arts), arts[0].get("uai/seq") if arts else None))
    lab.close()

    # --- r3-P2：正常解锁按账本原序，裁定不被推到终态后 ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "r3c", "seq": 1, "kind": "result_ready"},
               {"record": "VERDICT", "task_id": "r3c", "verdict": "通过"},
               {"record": "VERDICT", "task_id": "r3c", "verdict": "不通过"},
               {"record": "TASK_EVENT", "task_id": "r3c", "seq": 2, "kind": "completed"},
               {"record": "TASK_STATE", "task_id": "r3c", "state": "SUCCEEDED",
                "result_pointer": {"uri": "file:///r"}})
    d = lab.drain()
    f2 = lab.flush()
    lines = lab.lines()
    order = [(e.get("uai/source") or e.get("kind"),
              (e.get("status") or {}).get("message", {}).get("parts", [{}])[0].get("text")
              if e.get("status") else None,
              e.get("uai/seq"))
             for e in lines
             if e.get("uai/source") == "VERDICT" or e.get("uai/seq") == 2]
    check("r3-P2 解锁后裁定与终态按账本原序（裁定先于/伴随终态，非推后）",
          len(order) >= 2 and order[0][0] == "VERDICT"
          and order[-1][2] == 2 and any(o[0] == "status_update" and o[2] == 2 for o in order),
          str(order))
    lab.close()

    # ===== §7 矩阵补充 =====
    # --- N1 撕裂行 fail-closed（独立 lab：不复用已 close 的 tmp） ---
    tmp1 = tempfile.TemporaryDirectory()
    base1 = Path(tmp1.name)
    (base1 / "data").mkdir()
    led1 = base1 / "data" / "ledger.jsonl"
    led1.touch()
    out1 = base1 / "data" / "o.jsonl"
    b1 = EventBridge(base1, out1, experimental_test_only=True)
    with led1.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "TASK_EVENT", "task_id": "n1", "seq": 1,
                            "kind": "accepted"}) + "\n")
    d1 = b1.drain()
    b1.write(d1)
    b1.close()
    with out1.open("a", encoding="utf-8") as f:
        f.write('{"kind":')
    try:
        EventBridge(base1, out1, experimental_test_only=True)
        check("N1 outbox 撕裂行 → fail-closed", False, "未抛错")
    except RuntimeError as e:
        check("N1 outbox 撕裂行 → fail-closed", "incomplete_outbox" in str(e), str(e)[:60])
    try:
        tmp1.cleanup()
    except PermissionError:
        pass

    # --- flush 后真指针到达：不重复补发 artifact ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "fp", "seq": 1, "kind": "result_ready"},
               {"record": "TASK_EVENT", "task_id": "fp", "seq": 2, "kind": "completed"})
    lab.drain()
    lab.flush()
    lab.append({"record": "TASK_STATE", "task_id": "fp", "state": "SUCCEEDED",
                "result_pointer": {"uri": "file:///x"}})
    d1 = lab.drain()
    n_arts = sum(1 for e in d1 if e.get("kind") == "artifact_update")
    lab.restart()
    d2 = lab.drain()
    n_arts2 = sum(1 for e in d2 if e.get("kind") == "artifact_update")
    check("§7-6 flush 后真指针到达：artifact 恰一次、重启不重复",
          n_arts == 1 and n_arts2 == 0, "%d/%d" % (n_arts, n_arts2))
    lab.close()

    # --- N3：TASK_STATE 挂起守卫（synth final 延迟） ---
    lab = Lab()
    lab.append({"record": "TASK_EVENT", "task_id": "n3", "seq": 1, "kind": "result_ready"},
               {"record": "TASK_STATE", "task_id": "n3", "state": "SUCCEEDED"})
    d = lab.drain()
    check("N3 挂起期间 TASK_STATE 不提前 synth final",
          not any(e.get("final") for e in d), str(texts(d)))
    lab.append({"record": "TASK_STATE", "task_id": "n3", "state": "SUCCEEDED",
                "result_pointer": {"uri": "file:///p"}})
    d2 = lab.drain()
    check("N3 指针到达后 synth final 发布",
          any(e.get("final") for e in d2), str(texts(d2)))
    lab.close()

    # --- INV-4a 尾部去重不误伤隔位回归 ---
    lab = Lab()
    lab.append({"record": "TASK_STATE", "task_id": "i4", "state": "SUCCEEDED"},
               {"record": "VERDICT", "task_id": "i4", "verdict": "通过"},
               {"record": "VERDICT", "task_id": "i4", "verdict": "不通过"},
               {"record": "VERDICT", "task_id": "i4", "verdict": "通过"})
    d = lab.drain()
    vt = [e["status"]["message"]["parts"][0]["text"] for e in d
          if e.get("uai/source") == "VERDICT"]
    check("INV-4a 尾部去重：隔位回归值不误伤（通过→不通过→通过）",
          vt == ["通过", "不通过", "通过"], str(vt))
    lab.close()

    # --- INV-6：state 与 outbox 不一致 → fail-closed（独立 lab） ---
    tmp2 = tempfile.TemporaryDirectory()
    base2 = Path(tmp2.name)
    (base2 / "data").mkdir()
    led2 = base2 / "data" / "ledger.jsonl"
    led2.touch()
    out2 = base2 / "data" / "o.jsonl"
    b2 = EventBridge(base2, out2, experimental_test_only=True)
    with led2.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "TASK_STATE", "task_id": "i6", "state": "SUCCEEDED"}) + "\n")
        f.write(json.dumps({"record": "VERDICT", "task_id": "i6", "verdict": "通过"}) + "\n")
    evs6 = b2.drain()
    b2.write(evs6)
    b2.close()
    if not out2.exists():
        check("INV-6 state 有 final 而 outbox 缺失 → fail-closed", False,
              "outbox 未生成（write 未被调到）")
        try:
            tmp2.cleanup()
        except PermissionError:
            pass
    else:
        # 人为破坏：删 outbox 但留 state
        out2.unlink()
        try:
            EventBridge(base2, out2, experimental_test_only=True)
            check("INV-6 state 有 final 而 outbox 缺失 → fail-closed", False, "未抛错")
        except RuntimeError as e:
            check("INV-6 state 有 final 而 outbox 缺失 → fail-closed",
                  "invariant_violation" in str(e), str(e)[:60])
        try:
            tmp2.cleanup()
        except PermissionError:
            pass

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
