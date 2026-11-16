"""B-01 自检：三轮「连续同一原因」失败才阻塞。

旧实现的缺口：只有一个裸整数 `failed_verifications >= 3`。
它分不清「同一件事连着卡三次」和「三个不同毛病累计三次」，也不知道中间
有没有成功过——所以既会把可修复的任务误判成死锁，也会让真正卡死的任务
靠每轮换个说法永远不阻塞。

本测试逐条钉住新语义（RoundsTracker）。
"""

from __future__ import annotations

import json
import sys

import unified as U

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-56s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    # ---------- 连续同一原因 ----------
    t = U.RoundsTracker()
    states = []
    for _ in range(2):
        states.append(t.record_failure(["gate:evidence"])["governance_state"])
    check("B-01 0–2 轮同一原因不阻塞", states == ["active", "active"], str(states))

    snap = t.record_failure(["gate:evidence"])
    check("B-01 第 3 轮同一原因 → blocked", snap["blocked"] is True,
          snap["governance_state"])
    check("B-01 阻塞原因写明是三轮连续口径",
          str(snap["blocked_reason"]).startswith("consecutive_rounds:3:"),
          str(snap["blocked_reason"]))
    check("B-01 连续计数到 3", snap["consecutive"] == 3, str(snap["consecutive"]))

    # ---------- 换了原因就不算连续 ----------
    t2 = U.RoundsTracker()
    t2.record_failure(["a"])
    t2.record_failure(["a"])
    s2 = t2.record_failure(["b"])
    check("B-01 2 同类 + 1 不同 → 不阻塞（连续才算）", s2["blocked"] is False,
          "consecutive=%d" % s2["consecutive"])

    t3 = U.RoundsTracker()
    for r in (["a"], ["b"], ["c"]):
        s3 = t3.record_failure(r)
    check("B-01 3 轮各不同原因 → 不阻塞", s3["blocked"] is False,
          "rounds=%d consecutive=%d" % (s3["rounds"], s3["consecutive"]))

    # ---------- 一次成功清零 ----------
    t4 = U.RoundsTracker()
    t4.record_failure(["x"])
    t4.record_failure(["x"])
    t4.record_success()
    t4.record_failure(["x"])
    t4.record_failure(["x"])
    s4 = t4.record_failure(["x"])
    check("B-01 成功清零后重新数满 3 轮才阻塞", s4["blocked"] is True
          and s4["rounds"] == 6, "rounds=%d" % s4["rounds"])

    t5 = U.RoundsTracker()
    t5.record_failure(["x"])
    t5.record_failure(["x"])
    t5.record_success()
    s5 = t5.record_failure(["x"])
    check("B-01 成功打断连续，2 轮后不阻塞", s5["blocked"] is False,
          "consecutive=%d" % s5["consecutive"])

    # ---------- 阻塞后不能靠换原因洗白 ----------
    t6 = U.RoundsTracker()
    for _ in range(3):
        t6.record_failure(["y"])
    s6 = t6.record_failure(["z"])
    check("B-01 已阻塞后换原因仍阻塞", s6["blocked"] is True, s6["governance_state"])

    # ---------- 解除只有两条路 ----------
    try:
        t6.clear_block("")
    except U.ModelError as e:
        check("B-01 人工解除必须给理由", "clear_block_requires_reason" in str(e), str(e)[:40])
    else:
        check("B-01 人工解除必须给理由", False, "空理由被接受")

    s7 = t6.clear_block("证据口径订正，重开一轮核验")
    check("B-01 人工解除生效", s7["blocked"] is False, s7["governance_state"])
    check("B-01 人工解除留审计（历史里能查到原阻塞原因）",
          any(h.get("outcome") == "cleared_by_human"
              and str(h.get("previous_blocked_reason", "")).startswith("consecutive_rounds:3:")
              for h in t6.history), "")

    t7 = U.RoundsTracker()
    for _ in range(3):
        t7.record_failure(["y"])
    s8 = t7.record_success()
    check("B-01 通过一轮即解除并记 unblocked", s8["blocked"] is False
          and any(h.get("unblocked") for h in t7.history), "")

    # ---------- 空原因也阻塞，但不编造原因 ----------
    t8 = U.RoundsTracker()
    for _ in range(3):
        t8.record_failure([])
    check("B-01 三轮说不清原因同样阻塞", t8.blocked is True,
          "blocker=%s" % t8.blocker)
    check("B-01 空原因归一为 unspecified（不编造）", t8.blocker == "unspecified",
          str(t8.blocker))

    # ---------- 落账本往返 ----------
    rec = t7.as_record()
    json.dumps(rec)                       # 必须可序列化
    back = U.RoundsTracker.from_record(json.loads(json.dumps(rec)))
    check("B-01 轮次状态可落账本并还原（重启后不归零）",
          back.rounds == t7.rounds and back.blocked == t7.blocked,
          "rounds=%d blocked=%s" % (back.rounds, back.blocked))
    t9 = U.RoundsTracker()
    t9.rounds = 2
    t9.consecutive = 2
    t9.blocker = U.blocker_fingerprint(["gate:x"])
    back2 = U.RoundsTracker.from_record(json.loads(json.dumps(t9.as_record())))
    s9 = back2.record_failure(["gate:x"])
    check("B-01 还原后接着数：第 3 轮即阻塞", s9["blocked"] is True,
          "consecutive=%d" % s9["consecutive"])

    # ---------- 进入治理态派生 ----------
    t10 = U.RoundsTracker()
    for _ in range(3):
        t10.record_failure(["gate:x"])
    check("B-01 derive_governance_state 吃到 rounds 口径",
          U.derive_governance_state("SUCCEEDED", "不通过", rounds=t10) == "blocked", "")
    check("B-01 阻塞优先于裁定结论（通过也盖不住）",
          U.derive_governance_state("SUCCEEDED", "通过", rounds=t10) == "blocked", "")
    check("B-01 非终态同样适用",
          U.derive_governance_state("RUNNING", None, rounds=t10) == "blocked", "")
    t11 = U.RoundsTracker()
    t11.record_failure(["gate:x"])
    check("B-01 未满三轮时不阻塞",
          U.derive_governance_state("SUCCEEDED", "通过", rounds=t11) == "closed", "")

    # ---------- 旧调用方兼容 ----------
    check("B-01 旧 failed_verifications 计数仍生效（兼容）",
          U.derive_governance_state("SUCCEEDED", "不通过", failed_verifications=3) == "blocked", "")
    check("B-01 传了 rounds 就以 rounds 为准（旧裸计数不越权）",
          U.derive_governance_state("SUCCEEDED", "通过", failed_verifications=9,
                                    rounds=t11) == "closed", "")

    # ---------- project_state 直通 ----------
    proj = U.project_state("SUCCEEDED", "通过", rounds=t10)
    check("B-01 project_state 投影出 blocked",
          proj["governance_state"] == "blocked", str(proj["governance_state"]))
    check("B-01 project_state 不给 rounds 时不受影响",
          U.project_state("SUCCEEDED", "通过")["governance_state"] == "closed", "")

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
