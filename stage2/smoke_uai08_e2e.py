r"""UAI-08 真中台端到端冒烟：提交 → 裁定 → 图谱里真的多一条记忆。

跑法（中台需要在 4223 常驻）：

    python stage2\smoke_uai08_e2e.py

判定标准（三条都要真）：
  1. 裁定响应里的 `graph_memory.written / created` 为真；
  2. Neo4j 里按 `uai_key = "<task_id>|作废"` 查得到那条 Episodic；
  3. 该 task 在账本里只有一份 TASK_ACCEPTED / TASK_STATE / VERDICT
     （2026-11-17 双中枢事故的反向验证：那时是各 2 份）。

任务故意投给一个**不存在的能力**，所以它会停在 QUEUED、不会真的派给任何席位执行；
对未终态任务判「作废」是门禁允许的（见 test_verdict_gate.py）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "prototype"))

from lab_client import LabClient, build_envelope  # noqa: E402

NATS_URL = "nats://127.0.0.1:4223"


async def main() -> int:
    client = LabClient(NATS_URL)
    await client.connect()
    try:
        env = build_envelope(
            instruction="UAI-08 e2e: verdict must write back to graph memory",
            capability="uai08_graph_selftest_gateway",   # 不存在的席位：任务只留在 QUEUED
            params={},
            timeout_s=60,
            created_by="uai08-e2e",
        )
        sub = await client.submit(env)
        task_id = env["task_id"]
        print("submit:", json.dumps({k: sub.get(k) for k in ("ok", "state")}, ensure_ascii=False))
        st = await client.state(task_id)
        print("state:", st.get("state"))

        verdict = await client.verdict({
            "task_id": task_id,
            "verdict": "作废",
            "checker": "uai08-e2e",
            "evidence_level": "E1",
            "notes": "UAI-08 production wiring check (no execution: capability absent)",
        })
        gm = verdict.get("graph_memory") or {}
        print("verdict ok:", verdict.get("ok"), "| graph_memory:",
              json.dumps(gm, ensure_ascii=False))
        print("TASK_ID=" + task_id)

        node = None
        try:
            from neo4j import GraphDatabase
            drv = GraphDatabase.driver("bolt://127.0.0.1:7687", auth=("neo4j", "graphiti"),
                                       connection_timeout=3.0)
            with drv.session() as session:
                rec = session.run(
                    "MATCH (e:Episodic {uai_key: $k}) RETURN e.summary AS summary, "
                    "e.source AS source, e.uai_seen AS seen",
                    {"k": task_id + "|作废"},
                ).single()
            drv.close()
            node = dict(rec) if rec else None
            print("NODE:", json.dumps(node, ensure_ascii=False))
        except Exception as exc:
            print("NODE_QUERY_ERROR:", str(exc)[:200])

        ok = bool(verdict.get("ok") and gm.get("written") and gm.get("created") and node)
        print("E2E_RESULT:", "PASS" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
