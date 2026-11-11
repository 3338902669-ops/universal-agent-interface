"""A-02 端到端验证：A2A 请求 → 信封 → hub.submit → 中枢受理 → 账本落 QUEUED。

前置：lab 已起（nats + hub），NATS_URL 指向它。
跑法：python test_a02_inbound.py
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
from pathlib import Path

import inbound_bridge

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:43321")
BASE = Path(os.environ.get("LAB_BASE", str(Path(__file__).resolve().parent.parent / "prototype")))

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def ledger_rows(task_id: str) -> list[dict]:
    p = BASE / "data" / "ledger.jsonl"
    if not p.exists():
        return []
    rows = []
    for line in io.open(p, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if r.get("task_id") == task_id:
            rows.append(r)
    return rows


def states_of(task_id: str) -> list[str]:
    return [r["state"] for r in ledger_rows(task_id) if r.get("record") == "TASK_STATE"]


async def main() -> int:
    req = {"message": {"messageId": "a02-msg-0001",
                       "parts": [{"text": "只读检查：请回一个 OK"}]}}
    # 实验室原型实现的是契约 v0.2；用 v1 会被中枢以 unsupported_contract_version 拒绝
    try:
        await inbound_bridge.submit(nats_url=NATS_URL, request=req, created_by="a02-test",
                                    target_interface="dsh", timeout_s=60,
                                    contract_version="1.0")
    except Exception as e:
        check("A-05 版本不匹配被中枢明确拒绝（不静默降级）",
              "unsupported_contract_version" in str(e), str(e)[:60])
    else:
        check("A-05 版本不匹配被中枢明确拒绝（不静默降级）", False, "竟然被接受了")

    out = await inbound_bridge.submit(
        nats_url=NATS_URL, request=req, created_by="a02-test",
        target_interface="dsh", timeout_s=60, contract_version="0.2",
    )
    task_id = out["task_id"]
    check("A-02 中枢受理", out["accepted"] is True, json.dumps(out["hub_response"], ensure_ascii=False)[:80])
    check("A-02 返回 task_id", bool(task_id), task_id)
    check("A-02 信封 11 必填齐全",
          all(k in out["envelope"] for k in
              ("contract_version", "envelope_id", "task_id", "idempotency_key", "target",
               "instruction", "timeout_s", "cls", "created_by", "created_at", "trace_id")), "")

    # 账本里应出现该任务且首态 QUEUED
    st = states_of(task_id)
    check("A-02 账本已落该任务", bool(st), str(st[:3]))
    check("A-02 首态为 QUEUED", bool(st) and st[0] == "QUEUED", str(st[:3]))

    # hub.state 可查
    try:
        state = await inbound_bridge.query_state(nats_url=NATS_URL, task_id=task_id)
        ok = bool(state)
        check("A-02 hub.state 可查回状态", ok, json.dumps(state, ensure_ascii=False)[:90])
    except Exception as e:
        check("A-02 hub.state 可查回状态", False, str(e)[:70])

    # 幂等：同 messageId 再投一次，应得到同一个 task_id（或至少不产生新任务）
    before = len(states_of(task_id))
    out2 = await inbound_bridge.submit(
        nats_url=NATS_URL, request=req, created_by="a02-test",
        target_interface="dsh", timeout_s=60, contract_version="0.2",
    )
    check("A-02 同 messageId 派生同一 idempotency_key",
          out2["envelope"]["idempotency_key"] == out["envelope"]["idempotency_key"], "")

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
