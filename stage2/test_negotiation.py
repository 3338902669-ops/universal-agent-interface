"""E-NEGOT: agent negotiation semantics test."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from negotiation import NegotiationResponse, NegotiationLog, AgentNegotiator, NegotiationLoop
from smart_router import SmartRouter, AgentProfile
import tempfile

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    # 1. Response 类型验证
    try:
        NegotiationResponse("reject", "a")  # reject 无 reason
        check("NEG-1 reject 必须带 reason", False, "竟然没报错")
    except ValueError:
        check("NEG-1 reject 必须带 reason", True)
    try:
        NegotiationResponse("delegate", "a")  # delegate 无 target
        check("NEG-2 delegate 必须有 target", False, "竟然没报错")
    except ValueError:
        check("NEG-2 delegate 必须有 target", True)

    # 2. AgentNegotiator 评估
    neg = AgentNegotiator("codex", ["code", "deploy", "test"], max_concurrent=2)
    r1 = neg.evaluate("写代码", "backend", required_skills=["code"])
    check("NEG-3 技能匹配接受", r1.response_type == "accept", r1.response_type)

    r2 = neg.evaluate("写代码", "backend", required_skills=["security"])
    check("NEG-4 技能不匹配拒绝", r2.response_type == "reject" and "skill_mismatch" in r2.reason, r2.reason[:50])

    neg2 = AgentNegotiator("claude", ["code"], max_concurrent=1, current_load=1)
    r3 = neg2.evaluate("写代码", "backend")
    check("NEG-5 容量满拒绝", r3.response_type == "reject" and "capacity_full" in r3.reason, r3.reason[:50])

    # 3. NegotiationLoop 集成测试（用真实 SmartRouter）
    work = Path(tempfile.mkdtemp())
    router = SmartRouter(work / "router.json")
    router.register(AgentProfile("codex", ["code", "deploy", "test", "frontend"], "impl"))
    router.register(AgentProfile("claude", ["review", "design"], "review"))
    router.register(AgentProfile("dsh", ["security", "audit"], "guard"))

    neg_codex = AgentNegotiator("codex", ["code", "deploy", "test", "frontend"])
    neg_claude = AgentNegotiator("claude", ["review", "design"], known_limits=["security"])
    neg_dsh = AgentNegotiator("dsh", ["security", "audit"])

    loop = NegotiationLoop(router, {"codex": neg_codex, "claude": neg_claude, "dsh": neg_dsh})

    # 4. 正常接受：frontend 任务 codex 接受
    result = loop.negotiate("修复前端 CSS 布局")
    check("NEG-6 正常接受", result["status"] == "accepted" and result["agent_id"] == "codex", str(result.get("status")))

    # 5. 协商拒绝后升级：security 任务 codex 拒绝（skill mismatch）→ dsh 接受
    result2 = loop.negotiate("安全审计")
    check("NEG-7 拒绝后路由到下一个", result2["status"] == "accepted" and result2["agent_id"] == "dsh",
          "agent=%s rejected=%s" % (result2.get("agent_id"), result2.get("rejected_agents")))

    # 6. 协商日志
    check("NEG-8 协商有留痕", len(result2.get("negotiation_log", "")) > 5, result2.get("negotiation_log","")[:80])

    # 7. 全部拒绝升级给人（capacity=0 独立场景）
    import tempfile as _tf
    router_reject = SmartRouter(Path(_tf.mkdtemp()) / "reject.json")
    router_reject.register(AgentProfile("agent-x", ["code"], "only agent, capacity=0"))
    neg_x = AgentNegotiator("agent-x", ["code"], max_concurrent=0)
    loop_reject = NegotiationLoop(router_reject, {"agent-x": neg_x})
    result3 = loop_reject.negotiate("做任何事")
    check("NEG-9 全部拒绝升级给人", result3["status"] == "escalated", str(result3.get("status")))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
