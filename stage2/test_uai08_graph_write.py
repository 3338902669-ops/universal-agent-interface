"""UAI-08 自检：裁定 → 团队记忆图谱的**生产写入点**。

之前 `record_outcome` 只有测试在调，真实裁定落完图谱什么也学不到。现在
`outcome_recorder.record_verdict_outcome()` 是 hub 裁定路径的唯一写入点，
本测试钉住它的四条底线：幂等、不写凭据、fail-open、真图谱上确实写得进去。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

os.environ["UAI_GRAPH_OUTCOMES"] = "0"      # 单元用例默认不碰真图谱

import outcome_recorder as OR                # noqa: E402
from hub import Hub, Task                    # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


class FakeBridge:
    """只记调用、不连图谱；可配置返回值或抛异常。"""

    def __init__(self, result=None, raises=None):
        self.calls = []
        self.result = result or {"ok": True, "created": True, "seen": 1}
        self.raises = raises

    def record_outcome(self, agent_id, task_desc, success, verdict, source=None, key=None):
        self.calls.append({"agent_id": agent_id, "task_desc": task_desc, "verdict": verdict,
                           "source": source, "key": key, "success": success})
        if self.raises:
            raise self.raises
        return self.result


def test_env_switch():
    os.environ["UAI_GRAPH_OUTCOMES"] = "0"
    b = FakeBridge()
    out = OR.record_verdict_outcome(task_id="t-env", verdict="通过", bridge=b)
    check("UAI-08 env 关闭时完全不写", out["written"] is False
          and out["reason"] == "disabled_by_env" and not b.calls, json.dumps(out))
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"


def test_guards():
    b = FakeBridge()
    out = OR.record_verdict_outcome(task_id="", verdict="通过", bridge=b)
    check("UAI-08 缺 task_id 不写、不抛", out["written"] is False
          and out["reason"] == "missing_task_id", json.dumps(out))
    out = OR.record_verdict_outcome(task_id="t-1", verdict="", bridge=b)
    check("UAI-08 缺 verdict 不写、不抛", out["reason"] == "missing_verdict", "")
    check("UAI-08 被拒的请求根本没碰图谱", b.calls == [], str(len(b.calls)))


def test_credential_filter():
    b = FakeBridge()
    out = OR.record_verdict_outcome(task_id="t-2", verdict="通过",
                                    description="deploy with api_key=sk-xxx to prod",
                                    bridge=b)
    check("UAI-08 任务文本像凭据 → 整条跳过", out["written"] is False
          and out["reason"] == "credential_like_text", json.dumps(out))
    check("UAI-08 凭据文本根本没送到图谱", b.calls == [], str(len(b.calls)))


def test_happy_and_key():
    b = FakeBridge()
    out = OR.record_verdict_outcome(task_id="t-3", verdict="带注通过",
                                    agent_id="codex", description="run tests",
                                    bridge=b)
    check("UAI-08 正常写入", out["written"] is True and out["created"] is True, json.dumps(out))
    check("UAI-08 幂等键 = task_id|verdict", out["key"] == "t-3|带注通过", str(out["key"]))
    call = b.calls[0]
    check("UAI-08 写入带 agent_id / verdict / source",
          call["agent_id"] == "codex" and call["verdict"] == "带注通过"
          and call["source"] == OR.DEFAULT_SOURCE, json.dumps(call)[:80])
    check("UAI-08 success 按裁定推导（带注通过=成功）", call["success"] is True, str(call["success"]))


def test_fail_open():
    b = FakeBridge(raises=RuntimeError("neo4j down"))
    out = OR.record_verdict_outcome(task_id="t-4", verdict="通过", bridge=b)
    check("UAI-08 图谱抛异常 → 不抛，给原因", out["written"] is False
          and str(out["reason"]).startswith("graph_error:"), json.dumps(out))
    b2 = FakeBridge(result={"ok": False, "reason": "constraint"})
    out2 = OR.record_verdict_outcome(task_id="t-5", verdict="通过", bridge=b2)
    check("UAI-08 图谱返回失败 → 不抛，给原因", out2["written"] is False
          and str(out2["reason"]).startswith("graph_rejected:"), json.dumps(out2))


def make_hub():
    base = Path(tempfile.mkdtemp())
    (base / "data").mkdir(parents=True, exist_ok=True)
    hub = Hub(base, "nats://127.0.0.1:43321")
    env = {"task_id": "t-hub-1", "trace_id": "tr-1", "idempotency_key": "idem-1",
           "created_by": "test", "instruction": "uai08 wiring check", "params": {}}
    t = Task(task_id="t-hub-1", envelope=env, trace_id="tr-1",
             idempotency_key="idem-1", state="SUCCEEDED")
    t.selected_interface = "codex"
    hub.tasks["t-hub-1"] = t
    return hub


class FakeMsg:
    def __init__(self, payload):
        self.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.reply = "reply"
        self.responses = []

    async def respond(self, data):
        self.responses.append(json.loads(data.decode("utf-8")))


def test_hub_wiring():
    # env 关闭时：真路径也要带出 graph_memory 字段，且不写图谱
    os.environ["UAI_GRAPH_OUTCOMES"] = "0"
    hub = make_hub()
    msg = FakeMsg({"task_id": "t-hub-1", "verdict": "通过", "checker": "tester"})
    asyncio.run(hub.on_verdict(msg))
    resp = msg.responses[-1] if msg.responses else {}
    check("UAI-08 hub 裁定响应带 graph_memory 字段",
          isinstance(resp.get("graph_memory"), dict), json.dumps(resp)[:80])
    check("UAI-08 关闭时 hub 侧原因是 disabled_by_env",
          (resp.get("graph_memory") or {}).get("reason") == "disabled_by_env",
          json.dumps(resp.get("graph_memory")))

    # 开启 + 假 bridge：证明 hub 真把裁定交给了写入点
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"
    fake = FakeBridge(result={"ok": True, "created": True, "seen": 1})
    OR._BRIDGE = fake
    try:
        hub2 = make_hub()
        msg2 = FakeMsg({"task_id": "t-hub-1", "verdict": "通过", "checker": "tester"})
        asyncio.run(hub2.on_verdict(msg2))
        resp2 = msg2.responses[-1] if msg2.responses else {}
        gm = resp2.get("graph_memory") or {}
        check("UAI-08 hub 把裁定写进图谱（走过真实 on_verdict）",
              gm.get("written") is True, json.dumps(gm))
        check("UAI-08 hub 传入的任务文本与执行者正确",
              bool(fake.calls) and fake.calls[0]["task_desc"] == "uai08 wiring check"
              and fake.calls[0]["agent_id"] == "codex",
              json.dumps(fake.calls[0])[:90] if fake.calls else "no call")
    finally:
        OR._BRIDGE = None


def test_real_graph():
    """真图谱（Neo4j 7687）：真写、真去重、真查得到，然后清掉测试节点。"""
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"
    OR._BRIDGE = None
    try:
        from neo4j import GraphDatabase
        drv = GraphDatabase.driver("bolt://127.0.0.1:7687",
                                   auth=("neo4j", "graphiti"), connection_timeout=2.0)
        drv.verify_connectivity()
    except Exception as exc:
        check("UAI-08 真图谱：Neo4j 可达", False, "skip: %s" % str(exc)[:60])
        os.environ["UAI_GRAPH_OUTCOMES"] = "0"
        return

    task_id = "uai08-selftest-%d" % int(time.time())
    key = task_id + "|通过"
    first = OR.record_verdict_outcome(task_id=task_id, verdict="通过", agent_id="selftest",
                                      description="UAI-08 real graph selftest")
    second = OR.record_verdict_outcome(task_id=task_id, verdict="通过", agent_id="selftest",
                                       description="UAI-08 real graph selftest")
    check("UAI-08 真图谱首次写入 created=True",
          first.get("written") is True and first.get("created") is True, json.dumps(first))
    check("UAI-08 真图谱二次写入 created=False（幂等）",
          second.get("written") is True and second.get("created") is False, json.dumps(second))

    with drv.session() as s:
        n = s.run("MATCH (e:Episodic {uai_key: $k}) RETURN count(e) AS n", {"k": key}).single()["n"]
    check("UAI-08 真图谱里这个键只有 1 条记忆", int(n) == 1, "count=%s" % n)
    with drv.session() as s:
        s.run("MATCH (e:Episodic {uai_key: $k}) DETACH DELETE e", {"k": key})
        left = s.run("MATCH (e:Episodic {uai_key: $k}) RETURN count(e) AS n", {"k": key}).single()["n"]
    check("UAI-08 测试节点已清理", int(left) == 0, "remaining=%s" % left)
    drv.close()
    os.environ["UAI_GRAPH_OUTCOMES"] = "0"


def main() -> int:
    OR.reset_stats()
    test_env_switch()
    test_guards()
    test_credential_filter()
    test_happy_and_key()
    test_fail_open()
    test_hub_wiring()
    test_real_graph()

    s = OR.STATS
    check("UAI-08 统计计数有落点", s["written"] >= 2 and s["skipped"] >= 3 and s["failed"] >= 2,
          json.dumps(s, ensure_ascii=False))

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
