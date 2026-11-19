"""E-ROUTE: 智能路由器测试。"""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smart_router import SmartRouter, AgentProfile

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    work = Path(tempfile.mkdtemp())
    router = SmartRouter(work / "router.json")

    # 注册 3 个不同特长的 agent
    router.register(AgentProfile("codex", ["code", "deploy", "test", "frontend", "backend"], "实现主力", max_concurrent=2))
    router.register(AgentProfile("claude", ["code", "review", "frontend", "design"], "审查+前端", max_concurrent=1))
    router.register(AgentProfile("dsh", ["deploy", "security", "audit", "docs"], "看护+安全", max_concurrent=3))

    # 1. 基本路由：前端任务应该给有 frontend skill 的 agent
    d = router.route("修复登录页面 CSS 在 390px 下溢出的问题")
    check("ROUTE-1 前端任务路由", d["selected"] in ("codex", "claude"), d.get("selected",""))
    check("ROUTE-2 有解释", len(d.get("reason","")) > 10, d.get("reason","")[:80])
    check("ROUTE-3 返回候选列表", len(d.get("candidates",[])) >= 3, str(len(d.get("candidates",[]))))

    # 2. 安全任务应该给 dsh（只有 dsh 有 security skill）
    d2 = router.route("安全审计：检查 API endpoint 的权限控制")
    check("ROUTE-4 安全任务给 dsh", d2["selected"] == "dsh", d2.get("selected",""))

    # 3. 历史自适应：codex 连续成功 frontend → 后续 frontend 权重上升
    for _ in range(5):
        router.record_result("codex", "修复前端 CSS bug", True, 120)
    d3 = router.route("修复另一个前端页面样式")
    codex_score = next(c for c in d3["candidates"] if c["agent_id"] == "codex")
    check("ROUTE-5 历史成功提升分数", codex_score["breakdown"].get("history_success_rate") is not None
          and codex_score["breakdown"]["history_success_rate"] == 1.0,
          str(codex_score["breakdown"].get("history_success_rate")))

    # 4. 历史失败降低分数：claude 连续失败 frontend
    for _ in range(5):
        router.record_result("claude", "修复前端 CSS bug", False, 300)
    d4 = router.route("修复前端 CSS")
    claude_score = next(c for c in d4["candidates"] if c["agent_id"] == "claude")
    codex_score2 = next(c for c in d4["candidates"] if c["agent_id"] == "codex")
    check("ROUTE-6 历史失败降低分数", codex_score2["score"] > claude_score["score"],
          "codex=%.3f claude=%.3f" % (codex_score2["score"], claude_score["score"]))

    # 5. 低置信度升级
    router_narrow = SmartRouter(work / "narrow.json")
    router_narrow.register(AgentProfile("only-one", ["general"], "唯一的agent"))
    d5 = router_narrow.route("做某件事")
    check("ROUTE-7 低置信度升级给人", d5.get("needs_human") is True, "confidence=%.3f" % d5.get("confidence",0))

    # 6. 任务类型分类
    tt = router.route("部署到 Cloudflare Pages")
    check("ROUTE-8 任务类型识别", tt.get("task_type") == "deploy", tt.get("task_type"))

    # 7. 持久化：router 保存后新实例读回来
    router._save()
    router2 = SmartRouter(work / "router.json")
    check("ROUTE-9 持久化恢复", "codex" in router2.agents, str(list(router2.agents.keys())))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
