"""统一数据模型 v0.1 —— 三仓共用语义的最小实现。

依据：`~/.shared/UNIFIED-MODEL-v0.1.md`

核心决定：
  1. 状态是两根轴：`execution_state`（UAI 拥有）与 `governance_state`（AO 拥有）。
     `SUCCEEDED` 不决定治理关闭；治理关闭由裁定决定。
  2. 证据 = 等级（AO 的 E1–E4）× 载荷（UAI 的四字段），两者都要。
  3. `rigor`（L1–L3，证据下限）与 `cls`（副作用等级）正交，floor 取两者较大者。
  4. 审批 scope 就是 `action_digest`，不是自由文本。
  5. 审批人身份分 `asserted` / `authenticated`，当前只能是前者。

本模块是**提案实现**，不改动任何既有仓库文件。
"""

from __future__ import annotations

import hashlib
import json

# ---------- 轴一：执行状态（UAI 所有） ----------
EXECUTION_STATES = (
    "QUEUED", "DISPATCHED", "CLAIMED", "RUNNING",
    "SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED",
)

# ---------- 轴二：治理状态（AO 所有） ----------
GOVERNANCE_STATES = ("active", "blocked", "closed")

# ---------- 裁定（六值 canonical） ----------
CANONICAL_VERDICTS = ("通过", "带注通过", "条件通过", "不通过", "未核", "作废")
LEGACY_ALIASES = {"驳回": "不通过"}
CLOSING_VERDICTS = ("通过", "带注通过", "作废")   # 能让治理关闭的裁定

# ---------- A2A 投影 ----------
EXECUTION_TO_A2A = {
    "QUEUED": "TASK_STATE_SUBMITTED",
    "DISPATCHED": "TASK_STATE_SUBMITTED",
    "CLAIMED": "TASK_STATE_WORKING",
    "RUNNING": "TASK_STATE_WORKING",
    "SUCCEEDED": "TASK_STATE_COMPLETED",
    "FAILED": "TASK_STATE_FAILED",
    "TIMED_OUT": "TASK_STATE_FAILED",
    "CANCELLED": "TASK_STATE_CANCELED",
}

# ---------- 证据 ----------
GRADES = ("E1", "E2", "E3", "E4")
RIGOR_GRADE_FLOOR = {
    "L1": set(GRADES),
    "L2": {"E1", "E2"},
    "L3": {"E1"},
}
CLS_REQUIREMENTS = {
    "readonly": {"operations": (), "approval": False, "min_grade": "E3"},
    "reversible": {"operations": ("intent", "confirmed"), "approval": False, "min_grade": "E2"},
    "external": {"operations": ("intent", "confirmed", "unknown"), "approval": True, "min_grade": "E1"},
}


class ModelError(ValueError):
    """违反统一模型约束。"""


def canonicalize_verdict(value: str) -> str:
    return LEGACY_ALIASES.get(value, value)


def assert_writable_verdict(value: str) -> str:
    if value in LEGACY_ALIASES:
        raise ModelError("legacy_verdict_not_writable: %r → %r"
                         % (value, LEGACY_ALIASES[value]))
    if value not in CANONICAL_VERDICTS:
        raise ModelError("unknown_verdict: %r" % (value,))
    return value


# ---------- 治理状态派生 ----------
def derive_governance_state(execution_state: str, verdict: str | None = None,
                            blocked_reason: str | None = None,
                            failed_verifications: int = 0) -> str:
    """执行状态 + 裁定 → 治理状态。这是两根轴之间的唯一桥。

    规则：
      - 未到终态 → active
      - SUCCEEDED 且无裁定 → active（不是 closed！）
      - SUCCEEDED 且裁定属关闭类 → closed
      - SUCCEEDED 且「不通过」→ active（打回重做）
      - SUCCEEDED 且「未核」→ blocked（缺出口，显式阻塞）
      - FAILED / TIMED_OUT / CANCELLED → 治理按执行失败收口：
          * 若已判「作废」→ closed
          * 否则 → active
    """
    if execution_state not in EXECUTION_STATES:
        raise ModelError("unknown_execution_state: %r" % (execution_state,))
    # B-01：三轮核验失败即阻塞（对应 AO 的 C1/C2）
    if failed_verifications >= 3:
        return "blocked"
    if blocked_reason:
        return "blocked"

    if execution_state not in ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"):
        return "active"

    if verdict is None:
        return "active"

    v = assert_writable_verdict(canonicalize_verdict(verdict))

    if execution_state == "SUCCEEDED":
        if v in ("通过", "带注通过"):
            return "closed"
        if v == "作废":
            return "closed"
        if v == "未核":
            return "blocked"
        return "active"          # 条件通过 / 不通过

    # 执行失败类
    return "closed" if v == "作废" else "active"


def project_state(execution_state: str, verdict: str | None = None) -> dict:
    """对外投影：A2A 状态 + 两根轴 + 必要的「待核验」标记。"""
    if execution_state not in EXECUTION_TO_A2A:
        raise ModelError("unknown_execution_state: %r" % (execution_state,))
    out = {
        "a2a_state": EXECUTION_TO_A2A[execution_state],
        "execution_state": execution_state,
        "governance_state": derive_governance_state(execution_state, verdict),
    }
    if execution_state == "SUCCEEDED":
        out["message"] = (assert_writable_verdict(canonicalize_verdict(verdict))
                          if verdict else "待核验")
    if execution_state == "TIMED_OUT":
        out["reason_code"] = "timed_out"
    return out


# ---------- 证据下限 ----------
def grade_rank(g: str) -> int:
    """证据强度：E1 最强（4），E4 最弱（1）。数值越大越严格。"""
    if g not in GRADES:
        raise ModelError("unknown_grade: %r" % (g,))
    return 5 - int(g[1])


def floor(rigor: str, cls: str) -> dict:
    """返回该任务的最低证据要求。两轴取严。"""
    if rigor not in RIGOR_GRADE_FLOOR:
        raise ModelError("unknown_rigor: %r" % (rigor,))
    if cls not in CLS_REQUIREMENTS:
        raise ModelError("unknown_cls: %r" % (cls,))
    allowed = RIGOR_GRADE_FLOOR[rigor]
    need = CLS_REQUIREMENTS[cls]
    min_rank = grade_rank(need["min_grade"])
    allowed = {g for g in allowed if grade_rank(g) >= min_rank}
    if not allowed:
        allowed = {need["min_grade"]}
    return {
        "allowed_grades": sorted(allowed),
        "operations": list(need["operations"]),
        "approval_required": need["approval"],
        "payload_required": rigor == "L3" or cls == "external",
        "independent_reader_required": rigor == "L3",
    }


def validate_evidence(ev: dict, *, rigor: str, cls: str) -> None:
    """按 floor 校验一条证据记录；不合规抛 ModelError。"""
    req = floor(rigor, cls)
    grade = ev.get("grade")
    if grade not in GRADES:
        raise ModelError("missing_or_unknown_grade: %r" % (grade,))
    if grade not in req["allowed_grades"]:
        raise ModelError("grade_below_floor: %s 不在 %s 内（rigor=%s cls=%s）"
                         % (grade, req["allowed_grades"], rigor, cls))

    # AO 的等级细则
    if grade == "E1":
        for f in ("command", "exit_code", "revision"):
            if ev.get(f) in (None, ""):
                raise ModelError("E1_missing_%s" % f)
    if grade == "E2":
        peer = ev.get("peer")
        if not peer:
            raise ModelError("E2_missing_peer")
        if peer == ev.get("asserted_by"):
            raise ModelError("E2_peer_is_author")
    if grade == "E3" and not ev.get("note"):
        raise ModelError("E3_missing_note")
    if grade == "E4" and not ev.get("note"):
        raise ModelError("E4_missing_plan")

    # UAI 的载荷细则
    payload = ev.get("payload") or {}
    if req["payload_required"]:
        for f in ("uri", "sha256", "bytes", "media_type"):
            if payload.get(f) in (None, ""):
                raise ModelError("payload_missing_%s" % f)
    if payload.get("sha256"):
        s = str(payload["sha256"])
        if len(s) != 64 or any(c not in "0123456789abcdefABCDEF" for c in s):
            raise ModelError("payload_bad_sha256")

    if req["independent_reader_required"]:
        reader = ev.get("read_by")
        if not reader or reader == ev.get("asserted_by"):
            raise ModelError("L3_requires_independent_reader")


# ---------- 审批 ----------
def action_digest(envelope: dict) -> str:
    """scope 的来源：去掉 approval 后的规范化信封摘要。"""
    body = {k: v for k, v in envelope.items() if k != "approval"}
    canon = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def validate_approval(envelope: dict, credential: dict | None, *, cls: str) -> None:
    req = floor("L1", cls)
    if not req["approval_required"]:
        return
    if not credential:
        raise ModelError("external_requires_approval")
    want = action_digest(envelope)
    if credential.get("envelope_sha256") != want:
        raise ModelError("approval_scope_mismatch: %s != %s"
                         % (credential.get("envelope_sha256"), want))
    if credential.get("used"):
        raise ModelError("approval_already_used")
    auth = credential.get("approver_authenticity", "asserted")
    if auth not in ("asserted", "authenticated"):
        raise ModelError("bad_approver_authenticity: %r" % (auth,))
