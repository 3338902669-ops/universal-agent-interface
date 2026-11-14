"""E-SUB:订阅轮换留痕层测试。"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from subscription_rotator import (SubscriptionRotator, SubscriptionRegistry,
                                  looks_rate_limited)

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    work = Path(tempfile.mkdtemp())
    reg = work / "subscriptions.json"
    led = work / "data" / "ledger.jsonl"

    # 检测
    check("E-SUB 限流文案检出(rate limit)", looks_rate_limited("Error: You've hit your usage limit"))
    check("E-SUB 429 检出", looks_rate_limited("HTTP 429 Too Many Requests"))
    check("E-SUB 正常回复不误报", not looks_rate_limited("BRIDGE_OK"))

    rot = SubscriptionRotator(reg, led)

    # 单账号退化:限流→留痕但不切换
    rot.registry.add("acc-codex-1", "codex", "本机订阅")
    r1 = rot.on_agent_result("agent-1", "[System Error] usage limit reached", task_id="t-1")
    check("E-SUB 单账号限流留痕不切换", r1 == {"limited": True, "switched_to": None}, str(r1))
    rows = [json.loads(l) for l in led.read_text(encoding="utf-8").strip().split("\n")]
    check("E-SUB 两条事件落账本(LIMIT_HIT+EXHAUSTED)",
          [r["record"] for r in rows] == ["SUB_LIMIT_HIT", "SUB_EXHAUSTED"],
          str([r["record"] for r in rows]))

    # 双账号:限流→轮转切换
    rot.registry.add("acc-codex-2", "codex", "第二账号")
    r2 = rot.on_agent_result("agent-2", "429 quota exceeded", task_id="t-2")
    check("E-SUB 双账号限流切换", r2 == {"limited": True, "switched_to": "acc-codex-2"}, str(r2))
    check("E-SUB active 已轮转", rot.registry.active()["id"] == "acc-codex-2", "")

    # 连续限流→再切回
    r3 = rot.on_agent_result("agent-3", "rate limit hit", task_id="t-3")
    check("E-SUB 再次限流切下一账号", r3["switched_to"] == "acc-codex-1", str(r3))

    # 正常结果零事件
    n0 = len(led.read_text(encoding="utf-8").strip().split("\n"))
    rot.on_agent_result("agent-4", "BRIDGE_OK", task_id="t-4")
    n1 = len(led.read_text(encoding="utf-8").strip().split("\n"))
    check("E-SUB 正常结果零事件", n0 == n1, "")

    # 重复注册拒绝
    r5 = rot.registry.add("acc-codex-1", "codex")
    check("E-SUB 重复注册拒绝", r5 == {"added": False, "reason": "already_registered"}, str(r5))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
