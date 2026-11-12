"""P1 批次自检：A-06 context_id / B-01 三轮阻塞 / A-11 改派。

跑法：python test_p1_batch.py ；退出码 0 = 全通过。
"""

from __future__ import annotations

import sys

import inbound
import projection as P
import reassign as R
import unified as U

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-62s %s" % ("PASS" if cond else "FAIL", name, detail))


def expect(fn, name, needle):
    try:
        fn()
    except (R.ReassignError, U.ModelError, inbound.InboundError) as e:
        check(name, needle in str(e), str(e)[:60])
        return
    check(name, False, "未抛错")


def main() -> int:
    # ---------- A-06 context_id ----------
    req = {"contextId": "ctx-00000001",
           "message": {"messageId": "m-0000001", "parts": [{"text": "hello"}]}}
    env = inbound.envelope_from_a2a(req, created_by="u", target_interface="dsh")
    check("A-06 A2A contextId → 信封 context_id", env.get("context_id") == "ctx-00000001",
          str(env.get("context_id")))

    env2 = inbound.envelope_from_a2a(
        {"message": {"messageId": "m-0000002", "contextId": "ctx-00000002",
                     "parts": [{"text": "hi"}]}},
        created_by="u", target_interface="dsh")
    check("A-06 contextId 在 message 上也能取到", env2.get("context_id") == "ctx-00000002", "")

    task = P.project_task(task_id="task-0001", internal_state="RUNNING",
                          context_id=env["context_id"])
    check("A-06 投影到 A2A Task.context_id", task.get("context_id") == "ctx-00000001", "")

    env3 = inbound.envelope_from_a2a(
        {"message": {"messageId": "m-0000003", "parts": [{"text": "no ctx"}]}},
        created_by="u", target_interface="dsh")
    check("A-06 无 contextId 时为 None（不伪造）", env3.get("context_id") is None, "")

    # ---------- B-01 三轮阻塞 ----------
    check("B-01 0–2 次失败时不阻塞",
          all(U.derive_governance_state("SUCCEEDED", "不通过", failed_verifications=n) != "blocked"
              for n in (0, 1, 2)), "")
    check("B-01 第 3 次失败即 blocked",
          U.derive_governance_state("SUCCEEDED", "不通过", failed_verifications=3) == "blocked", "")
    check("B-01 阻塞优先于裁定结论",
          U.derive_governance_state("SUCCEEDED", "通过", failed_verifications=3) == "blocked",
          "通过也不能覆盖阻塞")
    check("B-01 执行态非终态时同样适用",
          U.derive_governance_state("RUNNING", None, failed_verifications=4) == "blocked", "")

    # ---------- A-11 改派 ----------
    lease = R.Lease(task_id="task-0002", interface_id="dsh", attempt=1, fence=1)
    new, ev = R.reassign(lease, to_interface="tianshu", reason="dsh crashed",
                         execution_state="CLAIMED", governance_state="active", now="2026-11-12T00:00:00Z")
    check("A-11 围栏递增", new.fence == lease.fence + 1, "%d→%d" % (lease.fence, new.fence))
    check("A-11 尝试号递增", new.attempt == lease.attempt + 1, "")
    check("A-11 改派事件记录被撤销的旧围栏",
          ev["revoked_fencing_generation"] == lease.fence and ev["fencing_generation"] == new.fence, "")
    check("A-11 事件带原因（C3）", ev["reason"] == "dsh crashed", "")

    r_old = R.accept_result(new, fence=1, attempt=1)
    check("A-11 旧围栏写入被拒", r_old["accepted"] is False and r_old["reason"] == "fencing_stale",
          r_old["reason"])
    r_new = R.accept_result(new, fence=2, attempt=2)
    check("A-11 新围栏写入被接受", r_new["accepted"] is True, "")

    expect(lambda: R.reassign(lease, to_interface="tianshu", reason="  "),
           "A-11 无原因改派被拒", "reason")
    expect(lambda: R.reassign(lease, to_interface="dsh", reason="x"),
           "A-11 改派给当前持有者被拒", "current_holder")
    expect(lambda: R.reassign(lease, to_interface="tianshu", reason="x", governance_state="blocked"),
           "A-11 阻塞任务不可改派（C2）", "blocked")
    expect(lambda: R.reassign(lease, to_interface="tianshu", reason="x", execution_state="SUCCEEDED"),
           "A-11 终态任务不可改派", "terminal")

    # ---------- C-05 核验者不得是实现者（AO V1） ----------
    def verdict_record(value, executor="dsh", verifier="tianshu", self_check=False, checked_at="2026-11-12T07:00:00Z"):
        iv = {"verifier": verifier, "self_check": self_check, "checked_at": checked_at}
        return {"task_id": "t-c05", "verdict": value, "decided_by_role": executor,
                "basis": {"independent_verification": iv}}

    U.validate_independent_verification(verdict_record("通过"), executor="dsh")
    check("C-05 独立核验者存在时「通过」成立", True)

    expect(lambda: U.validate_independent_verification({"verdict": "通过", "basis": {}}, executor="dsh"),
           "C-05 「通过」无独立核验被拒", "verdict_requires_independent_verification")
    expect(lambda: U.validate_independent_verification(
               verdict_record("通过", self_check=True), executor="dsh"),
           "C-05 self_check 不算独立核验", "self_check_is_not_independent_verification")
    expect(lambda: U.validate_independent_verification(
               verdict_record("通过", verifier="dsh"), executor="dsh"),
           "C-05 核验者=实现者被拒", "verifier_must_not_be_implementer")
    expect(lambda: U.validate_independent_verification(
               verdict_record("通过", checked_at=None), executor="dsh"),
           "C-05 缺核验时间被拒", "missing_checked_at")
    expect(lambda: U.validate_independent_verification(
               {"verdict": "未核", "basis": {"owner": "dsh"}}, executor="dsh"),
           "C-05 「未核」缺补齐计划被拒", "unverified_missing_completion_plan")
    expect(lambda: U.validate_independent_verification(
               {"verdict": "驳回", "basis": {}}, executor="dsh"),
           "C-05 历史别名不可写入", "驳回")

    U.validate_independent_verification({"verdict": "不通过", "basis": {}}, executor="dsh")
    check("C-05 「不通过」允许执行者自判（失败判定不需独立性）", True)
    U.validate_independent_verification(
        {"verdict": "未核", "basis": {"owner": "dsh", "min_action": "重跑一遍", "due_at": "2026-11-13T00:00:00Z"}},
        executor="dsh")
    check("C-05 「未核」三项齐全时成立", True)

    # ---------- C-04 账本是证据的唯一源 ----------
    import ledger_chain as LC

    rows = [{"record": "TASK_STATE", "task_id": "t1", "state": "QUEUED"},
            {"record": "VERDICT", "task_id": "t1", "verdict": "通过"}]
    rows = [LC.seal(rows[0], LC.GENESIS), LC.seal(rows[1], LC.GENESIS[:0] or LC.GENESIS)]
    good = {"evidence_id": "e1", "grade": "E1",
            "ledger": {"seq": 1, "record_sha256": U.ledger_record_digest(rows[1])}}
    before = dict(good)
    bound = U.bind_evidence_to_ledger(good, rows)
    check("C-04 证据挂到账本行上", bound["bound"]["verified"] is True and bound["bound"]["seq"] == 1,
          str(bound["bound"]["seq"]))
    check("C-04 不修改入参", good == before and "bound" not in good, str(list(good)))

    expect(lambda: U.bind_evidence_to_ledger({"evidence_id": "e2"}, rows),
           "C-04 无账本引用的证据被拒", "evidence_missing_ledger_ref")
    expect(lambda: U.bind_evidence_to_ledger(
               {"evidence_id": "e3", "ledger": {"seq": 9, "record_sha256": "a" * 64}}, rows),
           "C-04 行号越界被拒", "evidence_ledger_ref_dangling")
    expect(lambda: U.bind_evidence_to_ledger(
               {"evidence_id": "e4", "ledger": {"seq": 1, "record_sha256": "b" * 64}}, rows),
           "C-04 摘要不匹配被拒（双写漂移）", "evidence_ledger_digest_mismatch")

    store = [good, {"evidence_id": "e5", "ledger": {"seq": 1, "record_sha256": "c" * 64}}]
    report = U.check_evidence_store(store, rows)
    check("C-04 整库检查报冲突而非抛第一个错",
          report["ok"] is False and report["checked"] == 2 and len(report["conflicts"]) == 1,
          "%d conflicts" % len(report["conflicts"]))
    check("C-04 冲突里带证据 id 便于定位", report["conflicts"][0]["evidence_id"] == "e5",
          str(report["conflicts"][0]["reason"])[:40])

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
