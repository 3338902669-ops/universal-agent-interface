"""出站事件桥：跟随账本增量 → 投影为 A2A 事件 → 只追加出站信箱。

只读账本，绝不回写。心跳不投影。桥崩溃不影响账本。

跨记录联表（真账本逼出来的，缺一条就出错）：

  指针   `result_pointer` 挂在 `TASK_STATE` 上，`TASK_EVENT` 没有。
         → 等待指针，重启后重读账本恢复等待项。
  裁定   `VERDICT` 是独立记录，按账本顺序投影，不预取后续裁定。
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

F1–F4（天枢核验提出、隔离反例实锤后修复，2026-11-12）：

  F1b  指针可能永不到达 → completed 被永久扣压。
       修复：`force_pending_flush()` 操作方显式逃生口——发布 pending 中除
       result_ready 外的全部事件（终态不再被指针永久扣压），result_ready
       保留，指针迟到时仍能按 uai/seq 补发 artifact。
  F2   重复 seq 的终态事件且无 TASK_STATE 兜底 → 被静默吞掉。
       修复：契约 §11 要求重复消息形成审计记录 → 发 bridge_error
       （dup_terminal:<task>:<seq>），重启后经 synth 键去重不重复。
  F3   同一列表重复 write → outbox 重复行。
       修复：write 幂等（_written 集合按事件键去重；write 失败重建时一并清空）。
  F4   VERDICT 先于 TASK_STATE 到账 → 裁定被扣到终态才发布，独立时序丢失。
       修复：同批内存在该任务的终态 TASK_EVENT 时，synth_verdicts 以其为
       状态兜底（fallback_state），裁定按自己的 ts 先行发布（final=False），
       随后 completed 正常携带裁定发出终态（uai/seq 保留）。
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
        self._terminal_event_keys: set[tuple] = set()
        self._event_terminal_seen: dict[str, bool] = {}  # 事件路径已投递过终态的任务
        self._written: set[tuple] = set()   # F3：本进程已落盘的事件键，write 幂等
        self._fh = None
        self._pending_results: list[dict] = []
        self._load_emitted()
        if self.ledger.exists():
            self._fh = self.ledger.open("r", encoding="utf-8", errors="replace")

    # ---- 重启幂等：回读已发事件 ----
    def _load_emitted(self) -> None:
        if not self.out.exists():
            return
        with self.out.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.endswith("\n"):
                    raise RuntimeError("incomplete_outbox_line_requires_repair")
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                self._seen.add(_key(ev))
                # F3（agent-15 反例）：重启后调用方直接重试旧列表 write 时，
                # _written 必须也从 outbox 恢复，否则同一事件会写两遍。
                self._written.add(_key(ev))
                tid = ev.get("task_id") or (ev.get("task") or {}).get("id")
                if not tid:
                    continue
                if ev.get("final"):
                    self._final_emitted.add(tid)
                    if ev.get("uai/seq") is not None:
                        self._terminal_event_keys.add(_key(ev))
                    # 事件路径（非 synth）投递的终态：抑制口径要区分两者
                    if not ev.get("uai/synth"):
                        self._event_terminal_seen[tid] = True
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
            position = self._fh.tell()
            line = self._fh.readline()
            if not line:
                break
            if not line.endswith("\n"):
                self._fh.seek(position)
                break
            self.offset = self._fh.tell()
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
                if isinstance(record, dict):
                    record["_bridge_offset"] = position
                    out.append(record)
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

    def _pending_results_map(self) -> dict:
        """pending 里的 task_id → True（供 F2 判断"还挂着、不算被吞"）。"""
        return {p.get("task_id"): True for p in self._pending_results}

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
        if not task_id or not kind:
            return None
        if type(seq) is not int:
            synth = "invalid_seq:%s" % record.get("_bridge_offset")
            if ("synth", synth) in self._seen:
                return None
            self._seen.add(("synth", synth))
            return {"kind": "bridge_error", "task_id": task_id, "uai/synth": synth,
                    "error": "invalid_seq: expected integer", "uai/kind": kind}
        key = (task_id, seq)
        if key in self._seen:
            # F2：重复 seq 的终态事件不得静默吞掉——契约 §11 要求重复消息形成审计记录。
            # （有 TASK_STATE 兜底时 synth_terminal 会补发终态，这里只在"真的会被吞"时发审计。）
            if kind in TERMINAL_EVENT_KINDS and task_id not in self._final_emitted \
                    and task_id not in {t for t, s in self._states.items() if s in TERMINAL_STATES} \
                    and task_id not in self._pending_results_map():
                synth = "dup_terminal:%s:%d" % (task_id, seq)
                if ("synth", synth) not in self._seen:
                    self._seen.add(("synth", synth))
                    return {"kind": "bridge_error", "task_id": task_id,
                            "uai/synth": synth, "uai/seq": int(seq),
                            "uai/kind": kind,
                            "error": "duplicate_seq_terminal_without_state_fallback"}
            return None
        if kind == "result_ready" and not (record.get("result_ref") or self._pointers.get(task_id)):
            return None
        # 终态抑制的精确口径（两条 durability 测试 + test_io#25 共同锁定）：
        # 1) 同一把 (task_id, seq) 的终态事件不得重复投递（_seen 已拦）；
        # 2) 已 synth 过终态（_final_emitted 由 synth_terminal 记入、且非用户事件路径）
        #    的任务，后续 completed 不再发第二条 final —— 否则 SUCCEEDED+completed
        #    会出双终态（test_state_before_completed_does_not_duplicate_final）；
        # 3) 但「completed 事件本身投递过 final」后，账本顺序合法的新终态
        #    （SUCCEEDED 后出现 CANCELLED）必须发布（test_io#25），
        #    矛盾由消费方判定，桥不裁判。
        # 实现：_terminal_event_keys 记录的是"事件路径"已投递的键；
        # synth_terminal 路径只在任务从未有事件路径终态时抑制同/后续事件终态。
        if kind in TERMINAL_EVENT_KINDS:
            if (task_id, seq) in self._terminal_event_keys:
                return None
            if task_id in self._final_emitted and not self._event_terminal_seen.get(task_id):
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
            self._event_terminal_seen[task_id] = True   # 事件路径终态已投递
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
    def synth_verdicts(self, records: list[dict], *, defer_final: bool = False,
                       fallback_state: dict | None = None) -> list[dict]:
        """fallback_state（F4）：task_id → 同批内已见的终态执行态。

        VERDICT 先于 TASK_STATE 到账时，若同批里已有该任务的终态 TASK_EVENT
        （completed/failed/cancelled），就以它为状态兜底，让裁定按自己的时序
        立即发布（final=False，随后 completed 正常携带裁定发出终态），
        而不是报 verdict_without_state 把独立时序丢掉。
        """
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


            synth = "verdict:%s:%s" % (tid, rec.get("_bridge_offset", canon))
            if ("synth", synth) in self._seen:
                continue
            self._seen.add(("synth", synth))

            prev = self._verdict_emitted.get(tid)
            internal = self._states.get(tid)
            used_fallback = False
            if internal is None and fallback_state and tid in fallback_state:
                # F4：同批已有终态 TASK_EVENT —— 用它做状态兜底，保住裁定的独立时序。
                # 兜底时 final 必须为 False：真正的 final 由随后的 completed 事件携带
                # 裁定发出（单终态）；否则 completed+VERDICT 同批会出两条 final
                # （agent-15 复核反例实锤）。
                internal = fallback_state[tid]
                self._states[tid] = internal
                used_fallback = True
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
                    # used_fallback：兜底状态不是账本 TASK_STATE，final 让给随后的 completed
                    "final": internal in TERMINAL_STATES and not defer_final and not used_fallback,
                    "uai/synth": synth,
                    "uai/source": "VERDICT",
                }
            )
        return out

    def _process_records(self, records: list[dict], *, force: bool = False) -> list[dict]:
        """账本记录 → 出站事件的唯一处理循环（drain 与 force_pending_flush 共用）。

        force=False（drain）：result_ready 无指针时挂起等指针，同任务后续事件跟随挂起。
        force=True（flush）：忽略挂起守卫——这正是逃生口的意义（指针可能永不到达）；
        result_ready 无指针发不出 artifact，其余事件全部按权威语义处理。
        flush 场景（agent-15 第二轮反例）：自定义路由会静默丢非终态 TASK_EVENT、
        预取未来裁定颠倒时序。语义必须只有一份——flush 只是把 pending 塞回来
        重放这段权威循环。
        """
        emitted = []
        event_states = {"completed": "SUCCEEDED", "failed": "FAILED", "cancelled": "CANCELLED"}

        def terminal_candidates(batch):
            # Only a unique, not-yet-delivered sequence can represent a terminal.
            claimed = set(self._seen)
            candidates = set()
            for r in batch:
                if r.get("record") != EVENT_SOURCE or not r.get("task_id") or type(r.get("seq")) is not int:
                    continue
                key = (r["task_id"], r["seq"])
                if key in claimed:
                    continue
                claimed.add(key)
                if r.get("kind") in event_states:
                    candidates.add((r["task_id"], event_states[r["kind"]]))
            return candidates

        terminal_events = terminal_candidates(records)

        # F4：同批内终态 TASK_EVENT → task_id: 执行态（供 synth_verdicts 状态兜底）
        batch_terminal_state = {}
        for r in records:
            if (r.get("record") == EVENT_SOURCE and r.get("task_id")
                    and r.get("kind") in event_states):
                batch_terminal_state.setdefault(r["task_id"], event_states[r["kind"]])

        def process(rec):
            tid = rec.get("task_id")
            kind = rec.get("record")
            # invalid_seq 的记录不能被指针解析，扣进 pending 只会让审计错误
            # 永远出不来（N4 反例实锤）——先走 convert 发 bridge_error。
            if (rec.get("record") == EVENT_SOURCE and type(rec.get("seq")) is not int):
                self._absorb(rec)
                ev = self.convert(rec)
                if ev is not None:
                    emitted.append(ev)
                return
            # flush（force）模式：无指针的 result_ready 不占位——它发不出事件，
            # 却会把同任务后续真实事件（如 completed）堵在 pending 守卫后面
            # （durability test_pending_state_then_completed_prefers_real_event_after_restart）。
            # 指针到账时 artifact 由下轮 drain 正常补发。
            if force and rec.get("record") == EVENT_SOURCE and rec.get("kind") == "result_ready" \
                    and not (rec.get("result_ref") or self._pointers.get(tid)):
                return
            if not force and any(p.get("task_id") == tid for p in self._pending_results):
                self._pending_results.append(rec)
                if (kind == "TASK_STATE" and isinstance(rec.get("result_pointer"), dict)
                        and rec["result_pointer"].get("uri")):
                    self._pointers[tid] = rec["result_pointer"]
                    pending = [p for p in self._pending_results if p.get("task_id") == tid]
                    self._pending_results = [p for p in self._pending_results if p.get("task_id") != tid]
                    # 解锁重放顺序：先重放挂起的事件（completed 携带裁定发 final，
                    # uai/seq 保留），TASK_STATE 的 synth 放到最后且仅当该任务
                    # 仍缺终态——「real event 优先于 synth」（durability 反例锁定）。
                    pending_events = [p for p in pending if p.get("record") == EVENT_SOURCE]
                    pending_states = [p for p in pending if p.get("record") == "TASK_STATE"]
                    pending_verdicts = [p for p in pending if p.get("record") == "VERDICT"]
                    for p in pending_events:
                        process(p)
                    for p in pending_verdicts:
                        process(p)
                    for p in pending_states:
                        if p.get("task_id") not in self._final_emitted:
                            process(p)
                return
            self._absorb(rec)
            if kind == "TASK_STATE":
                if (tid, rec.get("state")) not in terminal_events:
                    emitted.extend(self.synth_terminal([rec]))
            elif kind == "VERDICT":
                emitted.extend(self.synth_verdicts([rec], defer_final=(tid, self._states.get(tid)) in terminal_events and tid not in self._final_emitted,
                                                   fallback_state=batch_terminal_state))
            else:
                if (kind == EVENT_SOURCE and rec.get("kind") in event_states
                        and type(rec.get("seq")) is int
                        and ((tid, rec["seq"]) not in self._seen
                             or (tid, rec["seq"]) in self._terminal_event_keys)
                        and (self._states.get(tid) not in TERMINAL_STATES
                             or self._states[tid] == event_states[rec["kind"]])):
                    self._states[tid] = event_states[rec["kind"]]
                ev = self.convert(rec)
                if ev is not None:
                    emitted.append(ev)
                elif (kind == EVENT_SOURCE and rec.get("kind") == "result_ready"
                      and type(rec.get("seq")) is int
                      and (tid, rec.get("seq")) not in self._seen):
                    self._pending_results.append(rec)
        for rec in records:
            process(rec)
        return emitted

    def drain(self) -> list[dict]:
        records = self.read_new()
        return self._process_records(records)

    def write(self, events: list[dict]) -> int:
        if not events:
            return 0
        self.out.parent.mkdir(parents=True, exist_ok=True)
        # F3：同一列表重复 write 不得产生重复行（操作方拿不到返回值时只能重发整批）。
        fresh = [ev for ev in events if _key(ev) not in self._written]
        if not fresh:
            return 0
        try:
            with self.out.open("a", encoding="utf-8") as f:
                for ev in fresh:
                    f.write(json.dumps(ev, ensure_ascii=False, sort_keys=True) + "\n")
            for ev in fresh:
                self._written.add(_key(ev))
        except Exception:
            # Rebuild from complete outbox lines; an incomplete tail fails closed.
            self._seen.clear()
            self._pointers.clear()
            self._verdicts.clear()
            self._bad_verdicts.clear()
            self._states.clear()
            self._verdict_emitted.clear()
            self._final_emitted.clear()
            self._terminal_event_keys.clear()
            self._event_terminal_seen.clear()
            self._pending_results.clear()
            self._written.clear()
            if self._fh:
                self._fh.seek(0)
            self.offset = 0
            self._load_emitted()
            raise
        return len(fresh)

    # ---- F1b：指针永久缺席时的显式逃生口 ----
    def force_pending_flush(self) -> list[dict]:
        """冲刷 pending 队列：把挂起记录**原序重放权威处理循环** _process_records。

        为什么不自己路由（agent-15 两轮反例的根因）：自定义路由与 drain 的
        权威语义必然漂移——第一轮丢 VERDICT/TASK_STATE，第二轮丢非终态
        TASK_EVENT（progress）且预取未来裁定颠倒时序。语义只有一份：
        flush = 把 pending 记录塞回同一段循环。

        result_ready 无指针时无法投影 artifact，循环会自然把它放回 pending。
        操作方（--flush-pending / 运维脚本）显式调用，drain 不自动触发，
        保持"等指针"作为默认语义。
        """
        pending = self._pending_results
        self._pending_results = []
        if not pending:
            return []
        # 按 pending 的账本偏移排序，保持挂起时的原始相对顺序
        pending.sort(key=lambda r: r.get("_bridge_offset", 0))
        return self._process_records(pending, force=True)

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
                    help="F1b 逃生口：发布 pending 中除 result_ready 外的事件"
                         "（指针永不到达时，终态不再被永久扣压）")
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
