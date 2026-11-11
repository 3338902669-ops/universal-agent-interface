"""统一数据模型 v0.1 自检。对应 UNIFIED-MODEL-v0.1.md 的关键断言。

跑法：python test_unified.py ；退出码 0 = 全通过。
"""

from __future__ import annotations

import sys

import unified as U

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-64s %s" % ("PASS" if cond else "FAIL", name, detail))


def expect(fn, name, needle=None):
    try:
        fn()
    except U.ModelError as e:
        ok = needle is None or needle in str(e)
        check(name, ok, str(e)[:70])
        return
    check(name, False, "no error")


# ---------- 两根轴 ----------
def test_axes():
    # SUCCEEDED 不决定治理关闭
    check("1 SUCCEEDED 无裁定 → governance=active（不是 closed）",
          U.derive_governance_state("SUCCEEDED") == "active", "")
    check("2 SUCCEEDED + 通过 → closed",
          U.derive_governance_state("SUCCEEDED", "通过") == "closed", "")
    check("3 SUCCEEDED + 带注通过 → closed",
          U.derive_governance_state("SUCCEEDED", "带注通过") == "closed", "")
    check("4 SUCCEEDED + 不通过 → active（打回重做）",
          U.derive_governance_state("SUCCEEDED", "不通过") == "active", "")
    check("5 SUCCEEDED + 未核 → blocked（缺出口）",
          U.derive_governance_state("SUCCEEDED", "未核") == "blocked", "")
    check("6 历史值「驳回」按「不通过」处理",
          U.derive_governance_state("SUCCEEDED", "驳回") == "active", "")
    check("7 AO 的 blocked 独立于执行状态",
          U.derive_governance_state("RUNNING", None, "three_failed_verifications") == "blocked", "")
    check("8 FAILED 无裁定 → active",
          U.derive_governance_state("FAILED") == "active", "")
    check("9 FAILED + 作废 → closed",
          U.derive_governance_state("FAILED", "作废") == "closed", "")

    p = U.project_state("SUCCEEDED")
    check("10 投影：SUCCEEDED 无裁定 → COMPLETED + 待核验",
          p["a2a_state"] == "TASK_STATE_COMPLETED" and p["message"] == "待核验", str(p["message"]))
    p2 = U.project_state("SUCCEEDED", "带注通过")
    check("11 投影：带注通过原样带出",
          p2["message"] == "带注通过" and p2["governance_state"] == "closed", "")
    p3 = U.project_state("TIMED_OUT")
    check("12 投影：TIMED_OUT → FAILED + timed_out",
          p3["a2a_state"] == "TASK_STATE_FAILED" and p3["reason_code"] == "timed_out", "")
    check("13 投影：任何状态都不出现 REJECTED",
          all(U.project_state(s, "作废")["a2a_state"] != "TASK_STATE_REJECTED"
              for s in U.EXECUTION_STATES), "")

    expect(lambda: U.derive_governance_state("DONE"), "14 未知执行态被拒（AO 的 done 不是执行态）", "unknown_execution_state")
    expect(lambda: U.derive_governance_state("SUCCEEDED", "驳回 "), "15 非法裁定被拒", "unknown_verdict")


# ---------- 证据与下限 ----------
def test_evidence():
    f1 = U.floor("L1", "readonly")
    check("16 L1+readonly 接受 E1–E3（E4 不够）、无审批",
          f1["allowed_grades"] == ["E1", "E2", "E3"] and not f1["approval_required"], str(f1["allowed_grades"]))
    f2 = U.floor("L3", "readonly")
    check("17 L3 只允许 E1，且要求载荷与独立读取者",
          f2["allowed_grades"] == ["E1"] and f2["payload_required"] and f2["independent_reader_required"], "")
    f3 = U.floor("L1", "external")
    check("18 external 强制 E1 + 审批 + 载荷",
          f3["allowed_grades"] == ["E1"] and f3["approval_required"] and f3["payload_required"], "")
    f4 = U.floor("L2", "reversible")
    check("19 L2+reversible 允许 E1/E2 且要 intent/confirmed",
          f4["allowed_grades"] == ["E1", "E2"] and f4["operations"] == ["intent", "confirmed"], "")

    good_payload = {"uri": "file:///r", "sha256": "a" * 64, "bytes": 3, "media_type": "text/plain"}
    e1 = {"grade": "E1", "command": "pytest -q", "exit_code": 0, "revision": "abc123",
          "asserted_by": "dsh", "payload": good_payload, "read_by": "tianshu"}
    U.validate_evidence(e1, rigor="L3", cls="readonly")
    check("20 合法 L3/E1 证据通过", True, "")

    expect(lambda: U.validate_evidence({**e1, "command": None}, rigor="L3", cls="readonly"),
           "21 E1 缺命令被拒", "E1_missing_command")
    expect(lambda: U.validate_evidence({**e1, "read_by": "dsh"}, rigor="L3", cls="readonly"),
           "22 L3 独立读取者不得是断言者", "L3_requires_independent_reader")
    expect(lambda: U.validate_evidence({**e1, "payload": {}}, rigor="L3", cls="readonly"),
           "23 L3 缺载荷被拒", "payload_missing")
    expect(lambda: U.validate_evidence({**e1, "grade": "E2", "peer": "dsh"}, rigor="L2", cls="readonly"),
           "24 E2 的同伴不得是作者", "E2_peer_is_author")
    expect(lambda: U.validate_evidence({"grade": "E3", "asserted_by": "x"}, rigor="L1", cls="readonly"),
           "25 E3 缺注记被拒", "E3_missing_note")
    expect(lambda: U.validate_evidence({**e1, "payload": {**good_payload, "sha256": "zz"}},
                                       rigor="L3", cls="readonly"),
           "26 坏哈希被拒", "payload_bad_sha256")
    expect(lambda: U.validate_evidence({**e1, "grade": "E2", "peer": "t"},
                                       rigor="L3", cls="readonly"),
           "27 L3 不接受 E2", "grade_below_floor")


# ---------- 审批 ----------
def test_approval():
    env = {"contract_version": "1.0", "task_id": "t1", "instruction": "deploy",
           "cls": "external", "approval": None}
    digest = U.action_digest(env)
    check("28 action_digest 与 approval 字段无关",
          digest == U.action_digest({**env, "approval": {"x": 1}}), digest[:12])
    cred = {"envelope_sha256": digest, "approver_authenticity": "asserted", "used": False}
    U.validate_approval(env, cred, cls="external")
    check("29 摘要匹配的凭据通过", True, "")
    expect(lambda: U.validate_approval(env, None, cls="external"),
           "30 external 无凭据被拒", "external_requires_approval")
    expect(lambda: U.validate_approval(env, {**cred, "envelope_sha256": "b" * 64}, cls="external"),
           "31 scope 不匹配被拒", "approval_scope_mismatch")
    expect(lambda: U.validate_approval(env, {**cred, "used": True}, cls="external"),
           "32 凭据已用过被拒", "approval_already_used")
    U.validate_approval(env, None, cls="readonly")
    check("33 readonly 不需要审批", True, "")


def main() -> int:
    test_axes()
    test_evidence()
    test_approval()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
