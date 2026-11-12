"""出站事件桥：跟随账本增量 → 投影为 A2A 事件 → 只追加出站信箱。

只读账本，绝不回写。心跳不投影。桥崩溃不影响账本。

跨记录联表（真账本逼出来的，缺一条就出错）：

  指针   `result_pointer` 挂在 `TASK_STATE` 上，`TASK_EVENT` 没有。
         → 先扫本批 `TASK_STATE` 建指针表，再转事件。
  裁定   `VERDICT` 是独立记录，且几乎总写在 `completed` 之后。
         → 先扫本批 `VERDICT`；终态事件 join 裁定；裁定晚到时补发一条。
  终态   `TIMED_OUT` 只出现在 `TASK_STATE` 上，没有对应事件。
         → 有终态状态、但从未发过终态事件的任务，补发一条。

幂等与去重（三条独立防线，缺一条就会重复）：

  1. `_seen` 按 `(task_id, seq)` / `("synth", name)` 去重，启动时**从出站信箱回读**，
     所以重启不会重发。
  2. `_verdict_emitted` 记住每个任务已经对外投递过的裁定值；
     值相同就不再 synth（避免"completed 已带裁定 + synth 再发一遍"）。
  3. `_final_emitted` 记住已经投递过终态的任务；
     `synth_terminal` 用它做**跨批次**守卫，而不只看当前批次。

增量方式：每次 `drain()` 从上次读到的位置续读；进程重启后从头重读，
由上面第 1 条保证不会重复投递。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import projection as P

EVENT_SOURCE = "TASK_EVENT"
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED")
TERMINAL_EVENT_KINDS = ("completed", "failed", "cancelled")


def _key(ev: dict) -> tuple:
    synth = ev.get("uai/synth")
    if synth:
        return ("synth", synth)
    tid = ev.get("task_id")
    # task 对象把 id 放在 task.id 里，没有顶层 task_id
    if not tid and isinstance(ev.get("task"), dict):
        tid = ev["task"].get("id")
    return (tid, ev.get("uai/seq"))


def _verdict_of_event(ev: dict) -> str | None:
    """从已投递事件里认出它的裁定值（供重启后重建状态）。"""
    if ev.get("uai/source") == "VERDICT" and isinstance(ev.get("status"), dict):
        v = (ev["status"].get("metadata") or {}).get("uai/verdict")
        if v:
            return v
    status = ev.get("status") or {}
    meta = status.get("metadata") or {}
    if meta.get("uai/verdict"):
        return meta["uai/verdict"]
    for part in ((status.get("message") or {}).get("parts") or []):
        if part.get("text") in P.CANONICAL_VERDICTS:
            return part["text"]
    return None


class EventBridge:
    def __init__(self, base: Path, out_path: Path, *, experimental_test_only: bool = False):
        if not experimental_test_only:
            raise RuntimeError("event_bridge_disabled_pending_ordering_and_durability_repair")
        self.ledger = Path(base) / "data" / "ledger.jsonl"
        self.out = Path(out_path)
        self.offset = 0
        self._seen: set[tuple] = set()
        self._pointers: dict[str, dict] = {}
        self._verdicts: dict[str, str] = {}
        self._bad_verdicts: dict[str, tuple] = {}
        self._states: dict[str, str] = {}
        self._verdict_emitted: dict[str, str] = {}
        self._final_emitted: set[str] = set()
        self._fh = None
        if self.ledger.exists():
            self._fh = self.ledger.open("r", encoding="utf-8", errors="replace")
        self._load_emitted()

    # ---- 重启幂等：回读已发事件 ----
    def _load_emitted(self) -> None:
        if not self.out.exists():
            return
        with self.out.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                self._seen.add(_key(ev))
                tid = ev.get("task_id") or (ev.get("task") or {}).get("id")
                if not tid:
                    continue
                if ev.get("final"):
                    self._final_emitted.add(tid)
                v = _verdict_of_event(ev)
                if v:
                    self._verdict_emitted[tid] = v

    # ---- 只读跟随 ----
    def read_new(self) -> list[dict]:
        if self._fh is None:
            if not self.ledger.exists():
                return []
            self._fh = self.ledger.open("r", encoding="utf-8", errors="replace")
        out = []
        while True:
            line = self._fh.readline()
            if not line:
                break
            self.offset += len(line)
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
        return out

    @staticmethod
    def _safe_verdict(value):
        """事件自带的裁定值：合法就归一，非法返回 None（不允许毁掉整条终态事件）。"""
        if not value:
            return None
        canon = P.canonicalize_verdict(str(value))
        try:
            return P.assert_writable_verdict(canon)
        except P.ProjectionError:
            return None

    # ---- 吸收旁路字段 ----
    def _absorb(self, record: dict) -> None:
        kind = record.get("record")
        tid = record.get("task_id")
        if not tid:
            return
        if kind == "TASK_STATE":
            rp = record.get("result_pointer")
            if isinstance(rp, dict):
                self._pointers[tid] = rp
            if record.get("state"):
                self._states[tid] = record["state"]
        elif kind == "VERDICT" and record.get("verdict"):
            raw = P.canonicalize_verdict(str(record["verdict"]))
            try:
                P.assert_writable_verdict(raw)
            except P.ProjectionError as e:
                # 非法裁定只记错，不进 _verdicts，避免污染 completed 的 join
                self._bad_verdicts[tid] = (str(record["verdict"]), str(e))
            else:
                self._verdicts[tid] = raw

    # ---- 转换单条事件 ----
    def convert(self, record: dict) -> dict | None:
        if record.get("record") != EVENT_SOURCE:
            return None
        task_id = record.get("task_id")
        seq = record.get("seq")
        kind = record.get("kind")
        if not task_id or seq is None or not kind:
            return None
        key = (task_id, int(seq))
        if key in self._seen:
            return None
        self._seen.add(key)

        event = {
            "task_id": task_id,
            "seq": int(seq),
            "kind": kind,
            "context_id": record.get("context_id"),
            "occurred_at": record.get("ts"),
            "result_ref": record.get("result_ref") or self._pointers.get(task_id),
            "reason_code": record.get("reason_code"),
            "verdict": self._safe_verdict(record.get("verdict")) or self._verdicts.get(task_id),
        }
        try:
            projected = P.project_event(event)
        except P.ProjectionError as e:
            return {
                "kind": "bridge_error",
                "task_id": task_id,
                "uai/seq": int(seq),
                "uai/kind": kind,
                "error": str(e),
            }
        if projected is None:
            return None  # heartbeat
        projected["uai/seq"] = int(seq)
        if projected.get("final"):
            self._final_emitted.add(task_id)
            v = _verdict_of_event(projected)
            if v:
                self._verdict_emitted[task_id] = v
        return projected

    # ---- 终态补发（跨批次用 _final_emitted 守卫）----
    def synth_terminal(self, records: list[dict]) -> list[dict]:
        out = []
        batch_terminal = {
            r.get("task_id")
            for r in records
            if r.get("record") == EVENT_SOURCE and r.get("kind") in TERMINAL_EVENT_KINDS
        }
        for rec in records:
            if rec.get("record") != "TASK_STATE":
                continue
            tid = rec.get("task_id")
            state = rec.get("state")
            if not tid or state not in TERMINAL_STATES:
                continue
            if tid in batch_terminal or tid in self._final_emitted:
                continue
            synth = "terminal:%s" % tid
            if ("synth", synth) in self._seen:
                continue
            self._seen.add(("synth", synth))
            try:
                status = P.project_status(
                    state,
                    verdict=self._verdicts.get(tid),
                    timestamp=rec.get("ts"),
                )
            except P.ProjectionError as e:
                out.append({"kind": "bridge_error", "task_id": tid, "uai/synth": synth,
                            "error": str(e)})
                continue
            # 外部只认 canonical 原因码；hub 内部原因串留在 uai/reason
            if state in P.FAILURE_REASON_CODE:
                status["message"] = {"parts": [{"text": P.FAILURE_REASON_CODE[state]}]}
            status["metadata"]["uai/reason"] = rec.get("reason") or rec.get("reason_category")
            self._final_emitted.add(tid)
            v = _verdict_of_event({"status": status})
            if v:
                self._verdict_emitted[tid] = v
            out.append(
                {
                    "kind": "status_update",
                    "task_id": tid,
                    "status": status,
                    "final": True,
                    "uai/synth": synth,
                    "uai/source": "TASK_STATE",
                }
            )
        return out

    # ---- 迟到裁定补发（同值不再重复）----
    def synth_verdicts(self, records: list[dict]) -> list[dict]:
        out = []
        for rec in records:
            if rec.get("record") != "VERDICT":
                continue
            tid = rec.get("task_id")
            raw = rec.get("verdict")
            if not tid or not raw:
                continue
            try:
                canon = P.assert_writable_verdict(P.canonicalize_verdict(str(raw)))
            except P.ProjectionError as e:
                err_synth = "verdict_error:%s:%s" % (tid, raw)
                if ("synth", err_synth) in self._seen:
                    continue
                self._seen.add(("synth", err_synth))
                out.append({"kind": "bridge_error", "task_id": tid,
                            "uai/synth": err_synth, "error": str(e)})
                continue

            # 同值已投递过 → 不再补发（这就是 30 个任务重复终态的根因）
            if self._verdict_emitted.get(tid) == canon:
                continue


            synth = "verdict:%s:%s" % (tid, canon)
            if ("synth", synth) in self._seen:
                continue
            self._seen.add(("synth", synth))

            prev = self._verdict_emitted.get(tid)
            internal = self._states.get(tid)
            if internal is None:
                err_synth = "verdict_without_state:%s:%s" % (tid, canon)
                if ("synth", err_synth) not in self._seen:
                    self._seen.add(("synth", err_synth))
                    out.append({"kind": "bridge_error", "task_id": tid,
                                "uai/synth": err_synth,
                                "error": "verdict_without_state: 账本里没有该任务的 TASK_STATE，"
                                         "不能凭空断言终态"})
                continue
            try:
                state_enum = P.state_of(internal)
            except P.ProjectionError:
                state_enum = "TASK_STATE_COMPLETED"
            meta = {"uai/state": internal, "uai/verdict": canon, "uai/checker": rec.get("checker")}
            if prev and prev != canon:
                meta["uai/supersedes"] = prev
            self._verdict_emitted[tid] = canon
            out.append(
                {
                    "kind": "status_update",
                    "task_id": tid,
                    "status": {
                        "state": state_enum,
                        "message": {"parts": [{"text": canon}]},
                        "timestamp": rec.get("ts"),
                        "metadata": meta,
                    },
                    "final": True,
                    "uai/synth": synth,
                    "uai/source": "VERDICT",
                }
            )
        return out

    def drain(self) -> list[dict]:
        records = self.read_new()
        for rec in records:
            self._absorb(rec)
        emitted = []
        for rec in records:
            ev = self.convert(rec)
            if ev is not None:
                emitted.append(ev)
        emitted.extend(self.synth_terminal(records))
        emitted.extend(self.synth_verdicts(records))
        return emitted

    def write(self, events: list[dict]) -> int:
        if not events:
            return 0
        self.out.parent.mkdir(parents=True, exist_ok=True)
        with self.out.open("a", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
        return len(events)

    def close(self):
        if self._fh:
            self._fh.close()
            self._fh = None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=".")
    ap.add_argument("--out", default="data/a2a-outbox.jsonl")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--interval", type=float, default=1.0)
    args = ap.parse_args()

    base = Path(args.base).resolve()
    out = Path(args.out)
    if not out.is_absolute():
        out = base / out

    bridge = EventBridge(base, out)
    total = 0
    try:
        while True:
            events = bridge.drain()
            n = bridge.write(events)
            total += n
            if args.once:
                print(json.dumps({"event": "bridge_once", "emitted": n, "total": total},
                                 ensure_ascii=False))
                break
            if n:
                print(json.dumps({"event": "bridge_flush", "emitted": n, "total": total},
                                 ensure_ascii=False), flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
