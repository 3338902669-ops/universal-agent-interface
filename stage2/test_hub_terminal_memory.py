"""P4b 自检：hub 终态事件自动入记忆（失败/超时/取消不得静默）。"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

os.environ["UAI_GRAPH_OUTCOMES"] = "0"      # 默认不碰真图谱

from hub import Hub, Task  # noqa: E402
import memory_pipeline      # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def make_hub(task_state="RUNNING"):
    base = Path(tempfile.mkdtemp())
    (base / "data").mkdir(parents=True, exist_ok=True)
    hub = Hub(base, "nats://127.0.0.1:43321")
    hub.memory_drain_s = 0
    env = {"task_id": "t-term-1", "trace_id": "tr-1", "idempotency_key": "idem-1",
           "created_by": "test", "instruction": "terminal memory check",
           "params": {}, "timeout_s": 60}
    task = Task(task_id="t-term-1", envelope=env, trace_id="tr-1",
                idempotency_key="idem-1", state=task_state)
    task.selected_interface = "tianshu"
    hub.tasks["t-term-1"] = task
    return hub, task


def run_terminal(state="TIMED_OUT", enabled="1"):
    os.environ["UAI_GRAPH_OUTCOMES"] = enabled
    hub, task = make_hub()
    calls = []
    original = memory_pipeline.record_terminal

    def fake(**kwargs):
        calls.append(kwargs)
        return {"enqueued": True, "written": True, "status": "delivered",
                "intent_id": "int-term-1"}

    memory_pipeline.record_terminal = fake
    try:
        async def scenario():
            await hub._terminal_fail(task, state, "retryable", "task_timeout", "hub")
            await asyncio.sleep(0.2)   # 让 spawn 出去的写记忆任务跑完

        asyncio.run(scenario())
    finally:
        memory_pipeline.record_terminal = original
    return calls, hub


def test_timeout_writes_memory():
    calls, hub = run_terminal("TIMED_OUT")
    check("TM-1 超时后派生了记忆写入", len(calls) == 1, str(len(calls)))
    if calls:
        call = calls[0]
        check("TM-2 记忆带 task_id", call.get("task_id") == "t-term-1", str(call.get("task_id")))
        check("TM-3 记忆带终态", call.get("state") == "TIMED_OUT", str(call.get("state")))
        check("TM-4 记忆带执行席位", call.get("agent_id") == "tianshu", str(call.get("agent_id")))
        check("TM-5 记忆带原因码", call.get("reason") == "task_timeout", str(call.get("reason")))
    check("TM-6 任务状态已落 TIMED_OUT", hub.tasks["t-term-1"].state == "TIMED_OUT",
          hub.tasks["t-term-1"].state)


def test_failed_and_cancelled():
    calls, _ = run_terminal("FAILED")
    check("TM-7 FAILED 也写记忆", len(calls) == 1 and calls[0]["state"] == "FAILED",
          str([c.get("state") for c in calls]))
    calls, _ = run_terminal("CANCELLED")
    check("TM-8 CANCELLED 也写记忆", len(calls) == 1 and calls[0]["state"] == "CANCELLED",
          str([c.get("state") for c in calls]))


def test_disabled():
    calls, _ = run_terminal("TIMED_OUT", enabled="0")
    check("TM-9 关闭开关后不写记忆", calls == [], str(len(calls)))


def test_already_terminal_no_double():
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"
    hub, task = make_hub(task_state="SUCCEEDED")
    calls = []
    original = memory_pipeline.record_terminal
    memory_pipeline.record_terminal = lambda **kw: calls.append(kw) or {"written": True}
    try:
        async def scenario():
            await hub._terminal_fail(task, "FAILED", "non_retryable", "x", "hub")
            await asyncio.sleep(0.2)

        asyncio.run(scenario())
    finally:
        memory_pipeline.record_terminal = original
    check("TM-10 已终态任务不重复写记忆", calls == [], str(len(calls)))
    os.environ["UAI_GRAPH_OUTCOMES"] = "0"


def main() -> int:
    test_timeout_writes_memory()
    test_failed_and_cancelled()
    test_disabled()
    test_already_terminal_no_double()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
