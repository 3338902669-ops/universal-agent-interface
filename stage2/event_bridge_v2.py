"""EventBridge v2 —— 账本增量 → A2A 事件流（规格 EVENTBRIDGE-V2-SPEC.md v2 的实现）。

与 v1 的结构性区别（三轮独立复核 + SPEC_PASS_WITH_NOTES 后重写）：

  1. 决策状态只有一份：`bridge-state.json`（原子重写：tmp + os.replace）。
     内存不保存决策；outbox 回读只用于启动校验（INV-6 / N1 撕裂行），不重建决策。
  2. 没有 pending 列表：挂起是 state 里的一位布尔（waiting_result）+ 每任务
     `backlog_offsets`（挂起期间吸收的账本偏移）。解除挂起 = 重放这些偏移上的
     记录——走**同一条** `_replay` 循环（INV-7：flush 无第二路由）。
  3. 顺序不变量 INV-1：一切按账本原序，禁止按记录类型分组重排。
  4. INV-4b：同一终态时点 final 单源（synth 先到则事件路径抑制，反之亦然）；
     裁定 update 的 final 不参与降级。
  5. INV-8：flush 放弃等待时，无指针的 result_ready 存入 `deferred_result`（state），
     指针到账后按原 seq 补发 artifact——不丢（R3 反例①）。

门禁：`experimental_test_only=False` 时抛错（保持默认禁用，与 v1 相同）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import projection as P

EVENT_SOURCE = "TASK_EVENT"
TERMINAL_STATES = ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED")
TERMINAL_EVENT_KINDS = ("completed", "failed", "cancelled")
EVENT_TO_STATE = {"completed": "SUCCEEDED", "failed": "FAILED", "cancelled": "CANCELLED"}
STATE_SCHEMA = "uai.bridge.state/2"


def _event_key(ev: dict) -> tuple:
    synth = ev.get("uai/synth")
    if synth:
        return ("synth", synth)
    tid = ev.get("task_id")
    if not tid and isinstance(ev.get("task"), dict):
        tid = ev["task"].get("id")
    return (tid, ev.get("uai/seq"))


def _verdict_of_event(ev: dict) -> str | None:
    status = ev.get("status") or {}
    meta = status.get("metadata") or {}
    if meta.get("uai/verdict"):
        return meta["uai/verdict"]
    for part in ((status.get("message") or {}).get("parts") or []):
        if part.get("text") in P.CANONICAL_VERDICTS:
            return part["text"]
    return None


def _new_task_state() -> dict:
    return {
        "waiting_result": False,
        "result_pointer": None,
        "last_state": None,
        "verdicts_emitted": [],       # 已对外发布的裁定序列（INV-4a 尾部去重依据）
        "pending_verdict": None,      # 挂起期间最近裁定（供 synth join）
        "backlog_offsets": [],        # 挂起期间吸收的账本偏移（INV-3 重放依据）
        "deferred_result": None,      # flush 放弃等待后仍无指针的 result_ready（INV-8）
        "final_emitted": False,
        "final_source": None,         # "event" | "synth" | None
        "terminal_slot": None,        # 已发 final 的终态时点标识（INV-4b）
        "seen_seqs": [],              # 已发布事件的任务内 seq（INV-5 重复审计依据）
    }


class EventBridge:
    def __init__(self, base: Path, out_path: Path, *, experimental_test_only: bool = False):
        if not experimental_test_only:
            raise RuntimeError("event_bridge_disabled_pending_ordering_and_durability_repair")
        self.base = Path(base)
        self.ledger = self.base / "data" / "ledger.jsonl"
        self.out = Path(out_path)
        self.state_path = self.out.parent / "bridge-state.json"
        self.state = self._empty_state()
        self._fh = None
        self._load_state()
        self._verify_startup()          # N1：outbox 撕裂行 fail-closed
        self._check_invariants()        # INV-6：state 与 outbox 一致性（含 outbox 缺失）
        if self.ledger.exists():
            self._fh = self.ledger.open("r", encoding="utf-8", errors="replace")
            self._fh.seek(self.state["ledger_committed_offset"])

    # ---------- state（规则 S1/S2） ----------
    @staticmethod
    def _empty_state() -> dict:
        return {"schema": STATE_SCHEMA, "ledger_committed_offset": 0, "tasks": {}}

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        if raw.get("schema") != STATE_SCHEMA:
            raise RuntimeError("bridge_state_schema_mismatch: %r" % raw.get("schema"))
        self.state = raw

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.state_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # ---------- 启动校验（INV-6 / N1） ----------
    def _verify_startup(self) -> None:
        if not self.out.exists():
            return
        with self.out.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if line and not line.endswith("\n"):
                    raise RuntimeError("incomplete_outbox_line_requires_repair")

    def _check_invariants(self) -> None:
        """INV-6：state 声称的 final 数不得超过 outbox 实际 final 行数；否则 fail-closed。"""
        if not self.out.exists():
            if any(t.get("final_emitted") for t in self.state["tasks"].values()):
                raise RuntimeError("invariant_violation_state_without_outbox")
            return
        finals = 0
        for line in self.out.open("r", encoding="utf-8", errors="replace"):
            if line.strip() and '"final": true' in line:
                finals += 1
        claimed = sum(1 for t in self.state["tasks"].values() if t.get("final_emitted"))
        if claimed > finals:
            raise RuntimeError("invariant_violation_final_count_mismatch: state=%d outbox=%d"
                               % (claimed, finals))

    # ---------- 账本读取 ----------
    def _read_offsets(self, offsets: list[int]) -> list[dict]:
        if not offsets or not self.ledger.exists():
            return []
        out = []
        with self.ledger.open("r", encoding="utf-8", errors="replace") as f:
            for off in sorted(offsets):
                f.seek(off)
                line = f.readline()
                if not line:
                    continue
                try:
                    rec = json.loads(line.strip())
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rec["_bridge_offset"] = off
                    out.append(rec)
        return out

    def _read_from(self, offset: int) -> list[dict]:
        if not self.ledger.exists():
            return []
        out = []
        with self.ledger.open("r", encoding="utf-8", errors="replace") as f:
            f.seek(offset)
            while True:
                pos = f.tell()
                line = f.readline()
                if not line or not line.endswith("\n"):
                    break                      # 账本半行：下次再读（追加中）
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if isinstance(rec, dict):
                    rec["_bridge_offset"] = pos
                    rec["_raw_line"] = line + "\n"
                    out.append(rec)
        return out

    # ---------- state 访问 ----------
    def _task(self, tid: str) -> dict:
        return self.state["tasks"].setdefault(tid, _new_task_state())

    def _resolved(self, tid: str) -> bool:
        """规格 §4：not waiting_result or result_pointer is not null。"""
        t = self.state["tasks"].get(tid)
        if t is None:
            return True
        return (not t.get("waiting_result")) or t.get("result_pointer") is not None

    # ---------- 吸收（不产生对外事件；INV-3「挂起的是发布不是处理」） ----------
    def _absorb(self, rec: dict) -> None:
        tid = rec.get("task_id")
        if not tid:
            return
        t = self._task(tid)
        kind = rec.get("record")
        if kind == "TASK_STATE":
            rp = rec.get("result_pointer")
            if isinstance(rp, dict) and rp.get("uri"):
                t["result_pointer"] = rp        # 指针到账 → resolved(tid) 变真
            t["last_state"] = rec.get("state") or t.get("last_state")

    @staticmethod
    def _safe_verdict(value):
        if not value:
            return None
        canon = P.canonicalize_verdict(str(value))
        try:
            return P.assert_writable_verdict(canon)
        except P.ProjectionError:
            return None

    # ---------- 单条投影（唯一投影入口） ----------
    def _project_event(self, rec: dict, t: dict) -> dict | None:
        task_id = rec.get("task_id")
        seq = rec.get("seq")
        kind = rec.get("kind")
        if type(seq) is not int:
            synth = "invalid_seq:%s" % rec.get("_bridge_offset")
            return {"kind": "bridge_error", "task_id": task_id, "uai/synth": synth,
                    "error": "invalid_seq: expected integer", "uai/kind": kind}
        if seq in t.get("seen_seqs", []):
            # INV-5：已发布过的 seq 重复 → 审计（仅终态且无兜底时才真的会被吞）
            if (kind in TERMINAL_EVENT_KINDS
                    and not t.get("final_emitted")
                    and t.get("last_state") not in TERMINAL_STATES
                    and not t.get("waiting_result")):
                synth = "dup_terminal:%s:%s" % (task_id, seq)
                return {"kind": "bridge_error", "task_id": task_id, "uai/synth": synth,
                        "uai/seq": seq, "uai/kind": kind,
                        "error": "duplicate_seq_terminal_without_state_fallback"}
            return None
        if kind == "result_ready" and not (rec.get("result_ref") or t.get("result_pointer")):
            return None
        # INV-4b：synth 路径已发过该任务终态 final（TASK_STATE 先于 completed 到账）
        # → completed 抑制（durability test_state_before_completed_does_not_duplicate_final
        #   锁定：两路径合计只 1 条 final）。反向（completed 先发）时 synth 由
        #   _maybe_synth_terminal 抑制。
        if kind in TERMINAL_EVENT_KINDS and t.get("final_emitted"):
            return None
        event = {
            "task_id": task_id,
            "seq": int(seq),
            "kind": kind,
            "context_id": rec.get("context_id"),
            "occurred_at": rec.get("ts"),
            "result_ref": rec.get("result_ref") or t.get("result_pointer"),
            "reason_code": rec.get("reason_code"),
            "verdict": self._safe_verdict(rec.get("verdict")) or (
                t["verdicts_emitted"][-1] if t.get("verdicts_emitted") else None),
        }
        try:
            projected = P.project_event(event)
        except P.ProjectionError as e:
            return {"kind": "bridge_error", "task_id": task_id, "uai/seq": int(seq),
                    "uai/kind": kind, "error": str(e)}
        if projected is None:
            return None                          # heartbeat
        projected["uai/seq"] = int(seq)
        t["seen_seqs"].append(seq)
        if projected.get("final"):
            # INV-4b：synth 路径已发过该任务终态 final（TASK_STATE 先于 completed 到账）
            # → completed 保留其 final（v1 权威行为，durability 锁定），这里不降级。
            t["final_emitted"] = True
            t["final_source"] = "event"
            v = _verdict_of_event(projected)
            if v and t["verdicts_emitted"][-1:] != [v]:
                t["verdicts_emitted"].append(v)
        return projected

    # ---------- VERDICT 发布 ----------
    def _emit_verdict(self, rec: dict, t: dict, fallback_state: dict) -> list[dict]:
        tid = rec.get("task_id")
        raw = rec.get("verdict")
        if not raw:
            return []
        try:
            canon = P.assert_writable_verdict(P.canonicalize_verdict(str(raw)))
        except P.ProjectionError as e:
            synth = "verdict_error:%s:%s" % (tid, raw)
            return [{"kind": "bridge_error", "task_id": tid, "uai/synth": synth,
                     "error": str(e)}]
        # INV-4a：尾部去重（隔位回归值不误伤）
        if t.get("verdicts_emitted")[-1:] == [canon]:
            return []
        internal = t.get("last_state")
        used_fallback = False
        if not internal and tid in fallback_state:
            # F4：同批已有终态 TASK_EVENT —— 兜底保住裁定的独立时序；
            # final 让给随后的 completed（agent-15 r2 反例）
            internal = fallback_state[tid]
            used_fallback = True
        if not internal:
            synth = "verdict_without_state:%s:%s" % (tid, canon)
            return [{"kind": "bridge_error", "task_id": tid, "uai/synth": synth,
                     "error": "verdict_without_state: 账本里没有该任务的 TASK_STATE，"
                              "不能凭空断言终态"}]
        try:
            state_enum = P.state_of(internal)
        except P.ProjectionError:
            state_enum = "TASK_STATE_COMPLETED"
        meta = {"uai/state": internal, "uai/verdict": canon, "uai/checker": rec.get("checker")}
        prev = t["verdicts_emitted"][-1] if t.get("verdicts_emitted") else None
        if prev and prev != canon:
            meta["uai/supersedes"] = prev
        t["verdicts_emitted"].append(canon)
        return [{
            "kind": "status_update",
            "task_id": tid,
            "status": {"state": state_enum,
                       "message": {"parts": [{"text": canon}]},
                       "timestamp": rec.get("ts"),
                       "metadata": meta},
            # used_fallback：兜底不是账本 TASK_STATE，final 让给 completed
            "final": internal in TERMINAL_STATES and not used_fallback,
            "uai/synth": "verdict:%s:%s" % (tid, rec.get("_bridge_offset", canon)),
            "uai/source": "VERDICT",
        }]

    # ---------- TASK_STATE synth ----------
    def _maybe_synth_terminal(self, rec: dict, t: dict) -> list[dict]:
        tid = rec.get("task_id")
        state = rec.get("state")
        if state not in TERMINAL_STATES:
            return []
        # INV-4b（v1 三轮复核后的权威语义）：
        #   * 事件路径（completed 等真实 TASK_EVENT）已发过该任务的终态 final
        #     → synth 一律抑制（P2 场景：completed final 后 TASK_STATE 不再发第二条）；
        #   * synth 先发过（TASK_STATE 先于 completed 到账）→ completed 保留其 final
        #     （durability test_state_before_completed_does_not_duplicate_final 的
        #      双路径区分由 _event_terminal_seen 承担）。
        if t.get("final_emitted") and t.get("final_source") == "event":
            return []
        try:
            status = P.project_status(
                state,
                verdict=t.get("pending_verdict") or (
                    t["verdicts_emitted"][-1] if t.get("verdicts_emitted") else None),
                timestamp=rec.get("ts"))
        except P.ProjectionError as e:
            synth = "terminal:%s:%s" % (tid, state)
            return [{"kind": "bridge_error", "task_id": tid, "uai/synth": synth,
                     "error": str(e)}]
        if state in P.FAILURE_REASON_CODE:
            status["message"] = {"parts": [{"text": P.FAILURE_REASON_CODE[state]}]}
        status.setdefault("metadata", {})["uai/reason"] = rec.get("reason") or rec.get("reason_category")
        t["final_emitted"] = True
        t["final_source"] = "synth"
        v = status.get("metadata", {}).get("uai/verdict")
        if v and t["verdicts_emitted"][-1:] != [v]:
            t["verdicts_emitted"].append(v)
        return [{
            "kind": "status_update", "task_id": tid, "status": status,
            "final": True, "uai/synth": "terminal:%s" % tid, "uai/source": "TASK_STATE",
        }]

    # ---------- 唯一处理循环（INV-7：drain 与 flush 共用） ----------
    def _replay(self, records: list[dict], *, force: bool = False) -> list[dict]:
        out = []
        # 同批终态 TASK_EVENT → fallback（F4：裁定先于状态到账时保住时序）
        fallback_state = {}
        for r in records:
            if (r.get("record") == EVENT_SOURCE and r.get("task_id")
                    and r.get("kind") in EVENT_TO_STATE):
                fallback_state.setdefault(r["task_id"], EVENT_TO_STATE[r["kind"]])

        for rec in records:
            tid = rec.get("task_id")
            kind = rec.get("record")
            if not tid:
                continue
            t = self._task(tid)

            if kind == EVENT_SOURCE and type(rec.get("seq")) is not int:
                # INV-5：非法 seq → 审计（不扣压——N4 反例）
                self._absorb(rec)
                ev = self._project_event(rec)
                if ev is not None:
                    out.append(ev)
                continue

            # INV-3：挂起的是发布，不是处理。吸收进 backlog，等解除后重放。
            # 注意：带指针的 TASK_STATE 先 absorb（指针就位即 resolved），
            # 这样它自己立刻走 synth 而不是进 backlog（N3 场景）。
            if t.get("waiting_result") and not self._resolved(tid):
                if kind == "TASK_STATE" and isinstance(rec.get("result_pointer"), dict) \
                        and rec["result_pointer"].get("uri"):
                    self._absorb(rec)          # 指针就位 → resolved → 本条立即处理
                    if t.get("backlog_offsets"):
                        backlog = self._read_offsets(t["backlog_offsets"])
                        t["backlog_offsets"] = []
                        out.extend(self._replay(backlog, force=force))
                    out.extend(self._maybe_synth_terminal(rec, t))
                else:
                    t["backlog_offsets"].append(rec["_bridge_offset"])
                    self._absorb(rec)
                continue

            if kind == EVENT_SOURCE and rec.get("kind") == "result_ready" \
                    and not (rec.get("result_ref") or t.get("result_pointer")):
                if force:
                    # INV-8：flush 放弃等待 → result_ready 存入 deferred（state），
                    # 指针到账后按原 seq 补发 artifact。不丢（R3 反例①）。
                    t["deferred_result"] = {"offset": rec["_bridge_offset"], "seq": rec.get("seq")}
                else:
                    t["waiting_result"] = True
                    t["backlog_offsets"].append(rec["_bridge_offset"])
                self._absorb(rec)
                continue

            self._absorb(rec)

            if kind == EVENT_SOURCE:
                ev = self._project_event(rec, t)
                if ev is not None:
                    out.append(ev)
                    # 解除挂起：backlog 里的裁定/状态按账本原序重放（INV-3）
                    if t.get("backlog_offsets"):
                        backlog = self._read_offsets(t["backlog_offsets"])
                        t["backlog_offsets"] = []
                        out.extend(self._replay(backlog, force=force))
            elif kind == "VERDICT":
                out.extend(self._emit_verdict(rec, t, fallback_state))
            elif kind == "TASK_STATE":
                out.extend(self._maybe_synth_terminal(rec, t))
                # 带指针的 TASK_STATE 解除挂起 → 重放 backlog（含 artifact）
                if t.get("backlog_offsets") and self._resolved(tid):
                    backlog = self._read_offsets(t["backlog_offsets"])
                    t["backlog_offsets"] = []
                    out.extend(self._replay(backlog, force=force))

        # INV-8：deferred result_ready 的指针到账 → 按原 seq 补发 artifact
        for tid, t in self.state["tasks"].items():
            dr = t.get("deferred_result")
            if dr and t.get("result_pointer"):
                rec = self._read_offsets([dr["offset"]])
                if rec:
                    ev = self._project_event(rec[0], t)
                    if ev is not None and ev.get("kind") == "artifact_update":
                        out.append(ev)
                t["deferred_result"] = None
        return out

    # ---------- 对外接口 ----------
    def drain(self) -> list[dict]:
        if self._fh is None:
            if not self.ledger.exists():
                return []
            self._fh = self.ledger.open("r", encoding="utf-8", errors="replace")
            self._fh.seek(self.state["ledger_committed_offset"])
        records = self._read_from(self.state["ledger_committed_offset"])
        if records:
            # committed offset 推进到**最后一条记录的行尾**：
            # 以行首 offset 重读会重复处理同一条记录（F2 重启重复审计的根因）。
            last = records[-1]
            self.state["ledger_committed_offset"] = last["_bridge_offset"] + \
                len((last.get("_raw_line") or "").encode("utf-8"))
        events = self._replay(records)
        if records:
            self._save_state()
        return events

    def force_pending_flush(self) -> list[dict]:
        """INV-7：flush = 解除全部等待 + 重放 backlog。没有第二条路由。"""
        changed = False
        for tid, t in self.state["tasks"].items():
            if t.get("waiting_result"):
                t["waiting_result"] = False     # 显式放弃等待指针（操作方决定）
                changed = True
        if not changed:
            return []
        # 重放所有挂起任务的 backlog（按账本原序）
        events = []
        for tid, t in self.state["tasks"].items():
            if t.get("backlog_offsets"):
                backlog = self._read_offsets(t["backlog_offsets"])
                t["backlog_offsets"] = []
                events.extend(self._replay(backlog, force=True))
        if events:
            self._save_state()
        return events

    def write(self, events: list[dict]) -> int:
        if not events:
            return 0
        self.out.parent.mkdir(parents=True, exist_ok=True)
        # F3（agent-15 r1）：调用方可能拿不到返回值而重发整批——write 必须幂等。
        # 幂等键入 state（S1），跨重启有效；调用方保留的旧列表重试不会产生重复行。
        # 键以 JSON 字符串形态存储：JSON 回读得到 list，与运行时 tuple 不相等会击穿去重。
        written = self.state.setdefault("written_keys", [])
        fresh = [ev for ev in events if json.dumps(_event_key(ev), ensure_ascii=False) not in written]
        if not fresh:
            return 0
        with self.out.open("a", encoding="utf-8") as f:
            for ev in fresh:
                f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
        for ev in fresh:
            written.append(json.dumps(_event_key(ev), ensure_ascii=False))
        self._save_state()
        # INV-6 在写入完成、state 与 outbox 对齐后校验
        self._check_invariants()
        return len(fresh)

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
    ap.add_argument("--flush-pending", action="store_true",
                    help="INV-7：解除全部等待并重放（指针可能永不到达时的操作方逃生口）")
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
            if args.flush_pending:
                events.extend(bridge.force_pending_flush())
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
