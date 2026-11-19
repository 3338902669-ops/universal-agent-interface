"""E-ORC: orchestrator full-chain test (injected fakes, no NATS)."""
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from negotiation import AgentNegotiator
from orchestrator import Orchestrator
from smart_router import AgentProfile, SmartRouter
from task_decomposer import TaskDecomposer

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def build_router(work):
    r = SmartRouter(work / "router.json")
    r.register(AgentProfile("codex", ["code", "deploy", "test", "frontend", "backend", "fix"], "impl"))
    r.register(AgentProfile("dsh", ["deploy", "security", "audit", "docs"], "guard"))
    return r


def make_fakes(state_sequence=None, verdict_result=None, submit_raises=False,
               state_raises=False):
    """Build injectable submit/state/verdict fakes."""
    calls = {"submit": [], "state": [], "verdict": []}

    async def submit_fn(request, created_by, target_interface, timeout_s):
        calls["submit"].append({"request": request, "agent": target_interface})
        if submit_raises:
            raise RuntimeError("hub down")
        return {"task_id": "t-%d" % len(calls["submit"]), "accepted": True}

    seq = list(state_sequence or [["SUCCEEDED"]])

    async def state_fn(task_id):
        calls["state"].append(task_id)
        if state_raises:
            raise RuntimeError("state query failed")
        bucket = seq.pop(0) if seq else ["SUCCEEDED"]
        return {"state": bucket[0]}

    async def verdict_fn(payload):
        calls["verdict"].append(payload)
        if verdict_result is not None:
            return verdict_result
        return {"ok": True, "verdict_record": {"three_layer_enforced": True}}

    return submit_fn, state_fn, verdict_fn, calls


def run(coro):
    return asyncio.run(coro)


def main():
    work = Path(tempfile.mkdtemp())

    # 1. 正常链路：拆解 -> 路由 -> 协商 -> 执行 -> 裁定
    router = build_router(work)
    negotiators = {
        "codex": AgentNegotiator("codex", ["code", "deploy", "test", "frontend", "backend"]),
        "dsh": AgentNegotiator("dsh", ["deploy", "security", "audit", "docs"]),
    }
    submit_fn, state_fn, verdict_fn, calls = make_fakes()
    orc = Orchestrator(router=router, negotiators=negotiators,
                       poll_interval_s=0.01, max_wait_s=5,
                       submit_fn=submit_fn, state_fn=state_fn, verdict_fn=verdict_fn)
    rep = run(orc.run("做一个完整的用户管理系统"))
    check("ORC-1 full chain ok", rep["overall"] == "ok", rep["overall"])
    check("ORC-2 subtasks executed", len(rep["results"]) == 5, str(len(rep["results"])))
    check("ORC-3 all accepted", rep["accepted"] == 5, str(rep["accepted"]))
    check("ORC-4 verdict issued per subtask", len(calls["verdict"]) == 5, str(len(calls["verdict"])))
    check("ORC-5 verifier != implementer",
          all(v["checker"] != r["agent"] for v, r in zip(calls["verdict"], rep["results"])),
          "")
    check("ORC-6 three_layer enforced passed through",
          all(r.get("three_layer_enforced") is True for r in rep["results"]), "")

    # 2. 协商拒绝 -> 换下一个候选
    work2 = Path(tempfile.mkdtemp())
    router2 = build_router(work2)
    neg2 = {
        "codex": AgentNegotiator("codex", ["code", "test"], known_limits=["安全", "审计", "security"]),
        "dsh": AgentNegotiator("dsh", ["security", "audit", "deploy", "docs", "backend", "frontend", "code", "test"]),
    }
    submit2, state2, verdict2, calls2 = make_fakes()
    orc2 = Orchestrator(router=router2, negotiators=neg2,
                        poll_interval_s=0.01, max_wait_s=5,
                        submit_fn=submit2, state_fn=state2, verdict_fn=verdict2)
    rep2 = run(orc2.run("对整个系统做安全审计"))
    picked = [r.get("agent") for r in rep2["results"] if r.get("agent")]
    check("ORC-7 rejection reroutes to another agent",
          "dsh" in picked, str(picked))
    check("ORC-8 negotiation trail recorded",
          any(len(r.get("negotiation") or []) > 0 for r in rep2["results"]), "")

    # 3. 提交失败不炸整条链
    work3 = Path(tempfile.mkdtemp())
    router3 = build_router(work3)
    s3, st3, v3, _ = make_fakes(submit_raises=True)
    orc3 = Orchestrator(router=router3, poll_interval_s=0.01, max_wait_s=2,
                        submit_fn=s3, state_fn=st3, verdict_fn=v3)
    rep3 = run(orc3.run("修复登录页面样式"))
    check("ORC-9 submit failure contained", rep3["overall"] == "failed", rep3["overall"])
    statuses3 = [r["status"] for r in rep3["results"]]
    check("ORC-10 submit failure recorded, downstream blocked",
          statuses3[0] == "submit_failed" and all(s == "blocked" for s in statuses3[1:]),
          str(statuses3))

    # 4. 执行失败 -> 不裁定，标 not_succeeded
    work4 = Path(tempfile.mkdtemp())
    router4 = build_router(work4)
    s4, st4, v4, calls4 = make_fakes(state_sequence=[["FAILED"]] * 10)
    orc4 = Orchestrator(router=router4, poll_interval_s=0.01, max_wait_s=2,
                        submit_fn=s4, state_fn=st4, verdict_fn=v4)
    rep4 = run(orc4.run("修复前端 CSS 溢出"))
    check("ORC-11 failed exec not adjudicated", len(calls4["verdict"]) == 0,
          str(len(calls4["verdict"])))
    statuses4 = [r["status"] for r in rep4["results"]]
    check("ORC-12 failed exec marked, downstream blocked",
          statuses4[0] == "not_succeeded" and all(s == "blocked" for s in statuses4[1:]),
          str(statuses4))

    # 5. 裁定被拒 -> verdict_rejected
    work5 = Path(tempfile.mkdtemp())
    router5 = build_router(work5)
    s5, st5, _, _ = make_fakes()
    async def v5(payload):
        return {"ok": False, "reason": "layer1_deterministic_failed"}
    orc5 = Orchestrator(router=router5, poll_interval_s=0.01, max_wait_s=2,
                        submit_fn=s5, state_fn=st5, verdict_fn=v5)
    rep5 = run(orc5.run("修复前端 CSS 溢出"))
    statuses5 = [r["status"] for r in rep5["results"]]
    check("ORC-13 rejected verdict surfaces, downstream blocked",
          statuses5[0] == "verdict_rejected" and all(s == "blocked" for s in statuses5[1:]),
          str(statuses5))
    check("ORC-14 rejection reason kept",
          rep5["results"][0].get("verdict_reason") == "layer1_deterministic_failed",
          str(rep5["results"][0].get("verdict_reason")))

    # 6. 依赖阻断：上游未通过 -> 下游 blocked
    work6 = Path(tempfile.mkdtemp())
    router6 = build_router(work6)
    s6, _, v6, _ = make_fakes()
    # 让所有子任务都失败
    async def st6_fail(task_id):
        return {"state": "FAILED"}
    orc6 = Orchestrator(router=router6, poll_interval_s=0.01, max_wait_s=2,
                        submit_fn=s6, state_fn=st6_fail, verdict_fn=v6)
    rep6 = run(orc6.run("做一个完整的系统"))
    statuses = [r["status"] for r in rep6["results"]]
    check("ORC-15 downstream blocked on upstream failure",
          "blocked" in statuses, str(statuses))
    check("ORC-16 overall not ok when blocked", rep6["overall"] != "ok", rep6["overall"])

    # 7. 需要人工：全部拒绝
    work7 = Path(tempfile.mkdtemp())
    router7 = SmartRouter(work7 / "router.json")
    router7.register(AgentProfile("only", ["code"], "solo"))
    neg7 = {"only": AgentNegotiator("only", ["code"], max_concurrent=0)}
    s7, st7, v7, _ = make_fakes()
    orc7 = Orchestrator(router=router7, negotiators=neg7, poll_interval_s=0.01,
                        max_wait_s=2, submit_fn=s7, state_fn=st7, verdict_fn=v7)
    rep7 = run(orc7.run("修复前端 CSS 溢出"))
    check("ORC-17 all rejected -> escalated", rep7["escalated"] >= 1, str(rep7["escalated"]))
    check("ORC-18 needs_human flagged", rep7["needs_human"] is True, "")

    # 9. 提交前 N 次失败后成功（hub 启动竞态）
    work8 = Path(tempfile.mkdtemp())
    router8 = build_router(work8)
    state8 = {"n": 0}
    async def flaky_submit(request, created_by, target_interface, timeout_s):
        state8["n"] += 1
        if state8["n"] < 3:
            raise RuntimeError("no responders")
        return {"task_id": "t-flaky", "accepted": True}
    s8, st8, v8, _ = make_fakes()
    orc8 = Orchestrator(router=router8, poll_interval_s=0.01, max_wait_s=2,
                        submit_retries=3, submit_backoff_s=0.01,
                        submit_fn=flaky_submit, state_fn=st8, verdict_fn=v8)
    rep8 = run(orc8.run("修复前端 CSS 溢出"))
    check("ORC-20 submit retries until success",
          rep8["results"][0]["status"] == "accepted"
          and rep8["results"][0].get("submit_attempts") == 3,
          str(rep8["results"][0].get("submit_attempts")))

    # 10. 重试用尽后失败
    work9 = Path(tempfile.mkdtemp())
    router9 = build_router(work9)
    s9, st9, v9, _ = make_fakes(submit_raises=True)
    orc9 = Orchestrator(router=router9, poll_interval_s=0.01, max_wait_s=2,
                        submit_retries=2, submit_backoff_s=0.01,
                        submit_fn=s9, state_fn=st9, verdict_fn=v9)
    rep9 = run(orc9.run("修复前端 CSS 溢出"))
    check("ORC-21 retries exhausted -> submit_failed",
          rep9["results"][0]["status"] == "submit_failed"
          and rep9["results"][0].get("submit_attempts") == 2,
          str(rep9["results"][0].get("submit_attempts")))

    # 8. 路由统计被回写（自适应）
    st = router.agents["codex"].stats
    check("ORC-19 router stats updated", len(st) > 0, str(st))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
