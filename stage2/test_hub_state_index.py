"""P6 自检：hub.on_state 必须是 O(1) 索引查询，不能每次扫全账本。

背景（2026-11-20 实测）：真实账本 113,539 行，on_state 每次扫两遍账本，
一次 1.3–6.4s。席位心跳是 2s 超时的 request/response，中枢一慢就会被判
INTERFACE_STALE（codex 近 1500 条账本里 278 次），派发随之 delivery_failed。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

import os
os.environ.setdefault("UAI_GRAPH_OUTCOMES", "0")

from hub import Hub  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


class Msg:
    def __init__(self, payload):
        self.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.reply = "reply"
        self.responses = []

    async def respond(self, data):
        self.responses.append(json.loads(data.decode("utf-8")))


def build_ledger(base: Path, task_id: str, n_states: int, n_events: int) -> None:
    data = base / "data"
    data.mkdir(parents=True, exist_ok=True)
    path = data / "ledger.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({
            "record": "TASK_ACCEPTED", "ts": "2026-11-20T00:00:00.000Z",
            "task_id": task_id, "trace_id": "tr", "envelope": {
                "task_id": task_id, "trace_id": "tr", "idempotency_key": "idem",
                "created_by": "test", "instruction": "x", "params": {},
                "envelope_sha256": "0" * 64},
        }, ensure_ascii=False) + "\n")
        for i in range(n_states):
            handle.write(json.dumps({
                "record": "TASK_STATE", "ts": "2026-11-20T00:00:%02d.000Z" % (i % 60),
                "task_id": task_id, "state": "RUNNING", "seq": i,
            }, ensure_ascii=False) + "\n")
        for i in range(n_events):
            handle.write(json.dumps({
                "record": "TASK_EVENT", "ts": "2026-11-20T00:00:00.000Z",
                "task_id": task_id, "seq": i, "kind": "progress",
            }, ensure_ascii=False) + "\n")


def test_state_is_fast():
    base = Path(tempfile.mkdtemp())
    build_ledger(base, "t-fast", 20000, 20000)
    hub = Hub(base, "nats://127.0.0.1:43321")
    hub.memory_drain_s = 0

    msg = Msg({"task_id": "t-fast"})
    started = time.time()
    asyncio.run(hub.on_state(msg))
    first = time.time() - started

    started = time.time()
    for _ in range(5):
        m = Msg({"task_id": "t-fast"})
        asyncio.run(hub.on_state(m))
    avg = (time.time() - started) / 5

    check("SI-1 首次查询 < 1.0s", first < 1.0, "%.3fs" % first)
    check("SI-2 单次查询 < 0.1s", avg < 0.1, "%.3fs" % avg)
    payload = msg.responses[-1] if msg.responses else {}
    check("SI-3 历史条数正确", len(payload.get("state_history") or []) == 20000,
          str(len(payload.get("state_history") or [])))
    check("SI-4 事件条数正确", len(payload.get("events") or []) == 20000,
          str(len(payload.get("events") or [])))


def test_state_sees_new_records():
    base = Path(tempfile.mkdtemp())
    build_ledger(base, "t-live", 5, 5)
    hub = Hub(base, "nats://127.0.0.1:43321")
    hub.memory_drain_s = 0
    hub.record("TASK_STATE", task_id="t-live", state="SUCCEEDED", seq=99)
    msg = Msg({"task_id": "t-live"})
    asyncio.run(hub.on_state(msg))
    payload = msg.responses[-1] if msg.responses else {}
    history = payload.get("state_history") or []
    check("SI-5 新写入立刻出现在索引里",
          bool(history) and history[-1].get("state") == "SUCCEEDED",
          str(history[-1].get("state") if history else None))
    check("SI-6 新记录条数 = 6", len(history) == 6, str(len(history)))


def test_unknown_task():
    base = Path(tempfile.mkdtemp())
    build_ledger(base, "t-x", 3, 3)
    hub = Hub(base, "nats://127.0.0.1:43321")
    hub.memory_drain_s = 0
    msg = Msg({"task_id": "nope"})
    asyncio.run(hub.on_state(msg))
    payload = msg.responses[-1] if msg.responses else {}
    check("SI-7 未知任务返回 not_found", payload.get("reason") == "not_found",
          json.dumps(payload))


def main() -> int:
    test_state_is_fast()
    test_state_sees_new_records()
    test_unknown_task()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
