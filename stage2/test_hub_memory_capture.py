"""P8 自检：hub 代写席位记忆 + 契约门禁 + 派单注入。"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path  # noqa: F401  (Path 在下方 UAI_MEMORY_DB 里用)

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

os.environ.setdefault("UAI_GRAPH_OUTCOMES", "1")
# 落在临时库：绝不往生产 outbox 写测试数据
os.environ["UAI_MEMORY_DB"] = str(Path(tempfile.mkdtemp()) / "hub-capture-outbox.db")

from hub import Hub, Task  # noqa: E402
import memory_bridge as MB  # noqa: E402

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


def make_hub(**contract):
    base = Path(tempfile.mkdtemp())
    (base / "data").mkdir(parents=True, exist_ok=True)
    hub = Hub(base, "nats://127.0.0.1:43321")
    hub.memory_drain_s = 0
    env = {"task_id": "t-cap", "trace_id": "tr", "idempotency_key": "idem",
           "created_by": "test", "instruction": "capture memory",
           "params": {"task_contract": contract} if contract else {},
           "timeout_s": 60}
    task = Task(task_id="t-cap", envelope=env, trace_id="tr",
                idempotency_key="idem", state="RUNNING")
    task.selected_interface = "codex"
    hub.tasks["t-cap"] = task
    return hub, task, base


def block_text():
    payload = {"kind": "milestone", "subject": "席位代写",
               "claim": "Codex 的结论已被中枢代写进团队记忆",
               "evidence_level": "E1", "evidence": "probe exit=0"}
    return "结论。" + MB.BLOCK_OPEN + json.dumps(payload, ensure_ascii=False) + MB.BLOCK_CLOSE


def write_result(directory: Path, text: str) -> dict:
    path = directory / "result.json"
    path.write_text(json.dumps({"type": "item.completed",
                                "item": {"type": "agent_message", "text": text}},
                               ensure_ascii=False) + "\n", encoding="utf-8")
    return {"uri": path.as_uri(), "media_type": "application/json"}


def test_capture_via_result_ready():
    hub, task, base = make_hub()
    pointer = write_result(base, block_text())
    msg = Msg({})
    event = {"kind": "result_ready", "seq": 1, "task_id": "t-cap",
             "fencing_generation": task.fencing_generation,
             "payload": {"pointer": pointer}}
    request = {"interface_id": "codex", "event": event}
    # 直接走内部路径，避免构造签名请求
    hub._capture_seat_memory(task, pointer, "codex")
    check("HC-1 结果里的块被代写", len(task.memory_intents) == 1,
          str(task.memory_intents))
    records = [row.get("record") for row in hub.ledger.records()]
    check("HC-2 账本留 MEMORY_CAPTURED", "MEMORY_CAPTURED" in records, str(records[-3:]))


def test_capture_rejects_bad_block():
    hub, task, base = make_hub()
    pointer = write_result(base, MB.BLOCK_OPEN + "not-json" + MB.BLOCK_CLOSE)
    hub._capture_seat_memory(task, pointer, "codex")
    records = [row.get("record") for row in hub.ledger.records()]
    check("HC-3 坏块不产生记忆", task.memory_intents == [], str(task.memory_intents))
    check("HC-4 坏块留 MEMORY_REJECTED", "MEMORY_REJECTED" in records, str(records[-3:]))


def verdict(hub, payload):
    msg = Msg(payload)
    asyncio.run(hub.on_verdict(msg))
    return msg.responses[-1] if msg.responses else {}


def test_gate_blocks_missing_memory():
    hub, task, _ = make_hub(memory_intent_required=True, scope="x", allowed_paths=[],
                            forbidden_actions=[], response_format="y", time_limit_s=10)
    task.state = "SUCCEEDED"
    resp = verdict(hub, {"task_id": "t-cap", "verdict": "通过", "checker": "tester"})
    check("HC-5 契约要求却无记忆 → 打回",
          resp.get("reason") == "memory_intent_missing", json.dumps(resp)[:110])


def test_gate_allows_when_captured():
    hub, task, _ = make_hub(memory_intent_required=True, scope="x", allowed_paths=[],
                            forbidden_actions=[], response_format="y", time_limit_s=10)
    task.state = "SUCCEEDED"
    task.memory_intents.append("seat:t-cap:0")
    resp = verdict(hub, {"task_id": "t-cap", "verdict": "通过", "checker": "tester"})
    check("HC-6 有记忆则放行", resp.get("ok") is True, json.dumps(resp)[:110])


def test_gate_absent_contract():
    hub, task, _ = make_hub()
    task.state = "SUCCEEDED"
    resp = verdict(hub, {"task_id": "t-cap", "verdict": "通过", "checker": "tester"})
    check("HC-7 无契约时不受影响", resp.get("ok") is True, json.dumps(resp)[:110])


def test_recall_suffix():
    sys.path.insert(0, str(PROTO))
    import interface as IF
    original = MB.recall_block
    MB.recall_block = lambda query, **kw: " 【团队记忆·自动注入】-事实A 【注入结束】"
    try:
        suffix = IF._recall_suffix({"instruction": "x"})
    finally:
        MB.recall_block = original
    check("HC-8 派单前言带上团队记忆", "自动注入" in suffix, suffix[:60])
    MB.recall_block = lambda query, **kw: ""
    try:
        empty = IF._recall_suffix({"instruction": "x"})
    finally:
        MB.recall_block = original
    check("HC-9 无记忆时不污染前言", empty == "", repr(empty))


def main() -> int:
    test_capture_via_result_ready()
    test_capture_rejects_bad_block()
    test_gate_blocks_missing_memory()
    test_gate_allows_when_captured()
    test_gate_absent_contract()
    test_recall_suffix()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
