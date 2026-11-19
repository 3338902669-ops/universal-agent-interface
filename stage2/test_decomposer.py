"""E-DECOMP: 任务自动拆解测试。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from task_decomposer import TaskDecomposer

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    d = TaskDecomposer(available_skills=["frontend", "backend", "testing", "deploy", "security", "debug", "docs"])

    # 1. full_stack 拆解
    r = d.decompose("做一个完整的用户管理系统，包含前端页面和后端API")
    check("DECOMP-1 full_stack 分类", r["template_used"] == "full_stack", r["template_used"])
    check("DECOMP-2 子任务数", len(r["subtasks"]) == 5, str(len(r["subtasks"])))
    check("DECOMP-3 有依赖关系", any(s["depends_on"] for s in r["subtasks"]), "")
    check("DECOMP-4 拓扑排序合法", len(r["dag_order"]) == 5, str(r["dag_order"]))
    check("DECOMP-5 全部可路由", len(r["unroutable"]) == 0, str(r["unroutable"]))

    # 2. DAG 依赖验证：deploy 应该在 test 之后
    order = r["dag_order"]
    deploy_idx = next(i for i, s in enumerate(r["subtasks"]) if s["type"] == "deploy")
    test_idx = next(i for i, s in enumerate(r["subtasks"]) if s["type"] == "testing")
    check("DECOMP-6 DAG: test 先于 deploy", order.index(test_idx) < order.index(deploy_idx),
          str(order))

    # 3. bug_fix 拆解
    r2 = d.decompose("修复用户登录崩溃的 bug")
    check("DECOMP-7 bug_fix 分类", r2["template_used"] == "bug_fix", r2["template_used"])
    check("DECOMP-8 bug_fix 三步", len(r2["subtasks"]) == 3, str(len(r2["subtasks"])))

    # 4. security_audit 拆解
    r3 = d.decompose("对整个系统做安全审计")
    check("DECOMP-9 security 分类", r3["template_used"] == "security_audit", r3["template_used"])

    # 5. 不可路由检测
    d2 = TaskDecomposer(available_skills=["frontend"])  # 只有 frontend
    r4 = d2.decompose("做一个完整的系统")
    check("DECOMP-10 不可路由检测", len(r4["unroutable"]) > 0, str(r4["unroutable"]))

    # 6. general 任务不拆（单步）
    r5 = d.decompose("帮我查一下天气")
    check("DECOMP-11 general 不拆", len(r5["subtasks"]) == 1, str(len(r5["subtasks"])))

    # 7. 递归拆解
    r6 = d.decompose_recursive("做一个完整的系统")
    has_children = any("children" in s for s in r6.get("subtasks", []))
    check("DECOMP-12 递归拆解有子层", has_children, str(has_children))

    # 8. 可解释性
    check("DECOMP-13 有拆解依据", len(r.get("explanation","")) > 20, r.get("explanation","")[:80])

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
