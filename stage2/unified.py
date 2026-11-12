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

# ---------- C-04：账本是证据的唯一源 ----------
class EvidenceSourceError(ModelError):
    """证据没有挂在账本记录上，或挂的记录与账本不一致（双写漂移）。"""


class IndependenceError(ModelError):
    """声称的「独立核验」并不独立（C-05 / AO V1）。"""


# 这三个裁定值等于对外声明「已被独立核验过」
REQUIRES_INDEPENDENT_VERIFICATION = ("通过", "带注通过", "条件通过")


def ledger_record_digest(record: dict) -> str:
    """账本记录的规范化摘要；与 ledger_chain.canonical 同一口径（排除两个链字段）。"""
    import ledger_chain as LC  # 同一口径，避免两套 canonical 漂移

    return hashlib.sha256(LC.canonical(record).encode("utf-8")).hexdigest()


def bind_evidence_to_ledger(evidence: dict, ledger_rows: list[dict]) -> dict:
    """C-04：证据必须引用账本里的某一行，且摘要对得上。

    `evidence["ledger"] = {"seq": <0 基行号>, "record_sha256": <64 hex>}`。
    行号按账本文件顺序（与 `ledger_chain.iter_records` 一致）。

    拒绝三种情况：
      * 没有 ledger 引用          → evidence_missing_ledger_ref（不允许「无主证据」）
      * 行号越界                  → evidence_ledger_ref_dangling
      * 摘要不匹配                → evidence_ledger_digest_mismatch（账本被改，或证据来自旁路存储）

    返回带 `bound` 的新 dict，**不修改入参**。
    """
    if not isinstance(evidence, dict):
        raise EvidenceSourceError("evidence_must_be_object")
    ref = evidence.get("ledger")
    if not isinstance(ref, dict):
        raise EvidenceSourceError("evidence_missing_ledger_ref")
    seq = ref.get("seq")
    digest = ref.get("record_sha256")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise EvidenceSourceError("evidence_ledger_seq_invalid: %r" % (seq,))
    if not isinstance(digest, str) or len(digest) != 64:
        raise EvidenceSourceError("evidence_ledger_digest_invalid: %r" % (digest,))
    if seq >= len(ledger_rows):
        raise EvidenceSourceError("evidence_ledger_ref_dangling: seq=%d rows=%d"
                                  % (seq, len(ledger_rows)))
    actual = ledger_record_digest(ledger_rows[seq])
    if actual != digest:
        raise EvidenceSourceError("evidence_ledger_digest_mismatch: %s != %s"
                                  % (digest[:16], actual[:16]))

    out = dict(evidence)
    out["bound"] = {"seq": seq, "record_sha256": actual, "verified": True}
    return out


def check_evidence_store(evidences: list[dict], ledger_rows: list[dict]) -> dict:
    """C-04：一次性检查一整个证据存储，返回冲突清单而不是抛第一个错。

    用途是发现「账本改了但证据队列没跟着改」（或反过来）的双写漂移。
    """
    conflicts = []
    for i, ev in enumerate(evidences):
        try:
            bind_evidence_to_ledger(ev, ledger_rows)
        except EvidenceSourceError as e:
            conflicts.append({"index": i, "evidence_id": (ev or {}).get("evidence_id"), "reason": str(e)})
    return {"ok": not conflicts, "checked": len(evidences), "conflicts": conflicts}


# ---------- C-05：核验者不得是实现者（AO V1） ----------
def validate_independent_verification(record: dict, *, executor: str) -> dict:
    """C-05：`通过 / 带注通过 / 条件通过` 必须带**独立**核验，且核验者≠执行者。

    规则（fail-closed）：
      1. 裁定值必须是 canonical 可写值；
      2. 上述三个值要求 `basis.independent_verification` 存在；
      3. 该对象必须写出 `verifier`、`checked_at`，且 `self_check` 不得为 True
         —— 自检不是核验，写 `self_check: true` 就是自认没有独立核验；
      4. `verifier` 不得等于 `executor`（AO V1：L2/L3 的核验者不得是实现者）；
      5. `不通过 / 作废` 允许执行者自判（失败判定不需要独立性）；
      6. `未核` 必须同时写负责人、最小补齐动作、绝对时限（契约 §10）。

    返回 basis（便于调用方复用），违反即抛 IndependenceError。
    """
    verdict = assert_writable_verdict(record.get("verdict"))
    basis = record.get("basis") or {}

    if verdict == "未核":
        missing = [k for k in ("owner", "min_action", "due_at") if not basis.get(k)]
        if missing:
            raise IndependenceError("unverified_missing_completion_plan: %s" % ",".join(missing))
        return basis

    if verdict not in REQUIRES_INDEPENDENT_VERIFICATION:
        return basis

    iv = basis.get("independent_verification")
    if not isinstance(iv, dict):
        raise IndependenceError("verdict_requires_independent_verification: %s" % verdict)
    if iv.get("self_check") is True:
        raise IndependenceError("self_check_is_not_independent_verification")
    verifier = iv.get("verifier")
    if not verifier:
        raise IndependenceError("independent_verification_missing_verifier")
    if not iv.get("checked_at"):
        raise IndependenceError("independent_verification_missing_checked_at")
    if verifier == executor:
        raise IndependenceError("verifier_must_not_be_implementer: %s" % verifier)
    return basis

# ---------- B-03 落地：驻留声明的强制点 ----------
def validate_residency(envelope: dict, actual_regions: list[str], *, strict: bool = True) -> dict:
    """§13.3 的强制点：声明了 `may_leave=false` 就不许写到 `store_in` 之外。

    失败语义（fail-closed）：
      * 信封完全没写 `residency`：`strict=True` 时拒（未声明 ≠ 无限制），否则放行并标 `undeclared`；
      * `may_leave` 缺省按 `false` 处理（与 schema 默认一致）；
      * 实际落地区域（`actual_regions`）里出现 `store_in` 之外的值 → 拒。

    返回审计对象（供写账本），违反即抛 ModelError（码 `residency_violation` / `residency_undeclared`）。
    """
    res = envelope.get("residency")
    if res is None:
        if strict:
            raise ModelError("residency_undeclared")
        return {"declared": False, "allowed": True, "store_in": [], "actual": list(actual_regions)}
    if not isinstance(res, dict):
        raise ModelError("residency_malformed")
    store_in = [r for r in (res.get("store_in") or [])]
    may_leave = bool(res.get("may_leave", False))
    if not store_in:
        raise ModelError("residency_missing_store_in")
    outside = [r for r in actual_regions if r not in store_in]
    if outside and not may_leave:
        raise ModelError("residency_violation: %s" % ",".join(sorted(set(outside))))
    return {
        "declared": True,
        "allowed": True,
        "store_in": store_in,
        "actual": list(actual_regions),
        "may_leave": may_leave,
        "retention_days": res.get("retention_days"),
        "outside": outside,
    }
