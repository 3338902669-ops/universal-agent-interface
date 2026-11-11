"""B-02 自检：UAI 对 AO 四条不变量的引用关系。

校验：
  1. 引用的 AO 要求 ID 真实存在且状态为 ENFORCED（需要兄弟仓；
     未克隆时 SKIP，不算失败）。
  2. UAI 侧确有对应载体（resource_key / 核验者字段 / verdict / 审批绑定）。
  3. AO 有而 UAI 曾缺的三项（三轮阻塞、阻塞拒绝操作、证据等级）现在确有实现。
"""

from __future__ import annotations

import io
import json
import os
import sys

import unified as U

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME = os.path.expanduser("~")
AO_CONF = os.path.join(HOME, "workspace", "projects", "joint-audit",
                       "agent-orchestra", "conformance.json")
REF_DOC = os.path.join(REPO, "protocol", "AO-INVARIANTS-REF.md")
ENVELOPE = os.path.join(REPO, "protocol", "schemas", "envelope.schema.json")
EVIDENCE = os.path.join(REPO, "protocol", "unified", "evidence-record.schema.json")

PASSES, FAILS, SKIPS = [], [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-62s %s" % ("PASS" if cond else "FAIL", name, detail))


def skip(name, why):
    SKIPS.append(name)
    print("SKIP  %-62s %s" % (name, why))


def load(p):
    with io.open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def main() -> int:
    doc = io.open(REF_DOC, encoding="utf-8").read() if os.path.exists(REF_DOC) else ""

    # ---------- 1. AO 侧 ----------
    if os.path.exists(AO_CONF):
        reqs = {r["id"]: r for r in load(AO_CONF)["requirements"]}
        for rid, clause in (("R1", "资源串行"), ("V1", "核验者独立"),
                            ("V4", "失败阻断"), ("A1", "外部动作审批")):
            r = reqs.get(rid)
            check("B-02 AO 要求 %s（%s）存在且 ENFORCED" % (rid, clause),
                  r is not None and r.get("status") == "ENFORCED",
                  str(r.get("status") if r else "missing"))
        for rid in ("R1", "V1", "V4", "A1"):
            check("B-02 引用文档提到 %s" % rid, rid in doc, "")
    else:
        skip("B-02 AO conformance 复核", "兄弟仓未克隆")

    # ---------- 2. UAI 侧载体 ----------
    env = load(ENVELOPE)
    props = env["properties"]
    check("B-02 载体 resource_key 存在", "resource_key" in props, "")
    check("B-02 载体 cls 存在", "cls" in props, "")
    check("B-02 载体 approval 存在", "approval" in props, "")
    check("B-02 载体 context_id 存在（A-06）", "context_id" in props, "")

    ev = load(EVIDENCE)
    check("B-02 证据 record 含 grade（A-07）", "grade" in ev["properties"], "")
    check("B-02 证据 record 含 peer（E2 要求）", "peer" in ev["properties"], "")
    check("B-02 证据 record 含 read_by（L3 独立读取者）", "read_by" in ev["properties"], "")

    # ---------- 3. AO 有 UAI 曾缺的三项 ----------
    check("B-02 三轮阻塞已实现（B-01）",
          U.derive_governance_state("SUCCEEDED", "不通过", failed_verifications=3) == "blocked", "")
    check("B-02 阻塞任务不可改派（C2）",
          "blocked_task_must_not_be_reassigned" in io.open(
              os.path.join(HERE, "reassign.py"), encoding="utf-8").read(), "")
    check("B-02 六值裁定与 AO 的 PASS 措辞有映射说明",
          "PASS" in doc or "verdict" in doc, "")

    print()
    print("RESULT: %d/%d passed（SKIP %d）" % (len(PASSES), len(PASSES) + len(FAILS), len(SKIPS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
