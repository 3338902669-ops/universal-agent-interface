"""E-PT:提示词模板注册表测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prompt_registry import PromptRegistry

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    reg = PromptRegistry()

    # 真实库全量解析
    items = reg.list()
    check("E-PT 真实库全量可解析", len(items) >= 6, "count=%d" % len(items))
    names = [i["name"] for i in items]
    check("E-PT name 前缀规范", all(n.startswith("prompt-template-") for n in names), str(names))

    # 加载与渲染
    p = reg.render("dev-implement", {"TASK": "修 bug", "SCOPE_FILES": "a.py",
                                     "CONSTRAINTS": "read-only"})
    check("E-PT 渲染变量替换完成", "修 bug" in p and "{TASK}" not in p
          and "a.py" in p and "read-only" in p, p[:60])
    check("E-PT 渲染保留纪律段", "DONE + 改动摘要" in p and "待核验" in p, "")

    # strict 缺变量报错
    try:
        reg.render("dev-review", {})
        check("E-PT strict 缺变量报错", False, "竟然没报错")
    except ValueError as e:
        check("E-PT strict 缺变量报错", "missing_variables" in str(e), str(e)[:60])

    # 非 strict 保留占位符（显式选择）
    p2 = reg.render("readonly-probe", {}, strict=False)
    check("E-PT 非 strict 保留占位", "{CHECK_ITEM}" in p2, "")

    # 不存在的模板
    try:
        reg.load("nope")
        check("E-PT 不存在模板报错", False, "竟然找到了")
    except FileNotFoundError:
        check("E-PT 不存在模板报错", True, "")

    # 变量清单可预查（派发前校验）
    item = next(i for i in items if i["name"] == "prompt-template-agent-orchestrate")
    check("E-PT 变量清单预查", set(item["variables"]) == {"GOAL", "AGENTS", "WRITE_BOUNDARY"},
          str(item["variables"]))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
