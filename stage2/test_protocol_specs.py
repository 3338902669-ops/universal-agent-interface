"""协议规格自检：evidence schema / floor 表 / 不保证清单 / 统一模型文档 一致性。

对应待完善项：A-07（证据等级×载荷）、A-08（不保证清单）、C-01（共享下限表）、
D-02/D-03（状态两轴与正交轴）、D-04/D-05（统一证据记录与责任矩阵）。

跑法：python test_protocol_specs.py ；退出码 0 = 全通过。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

import unified as U

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(HERE, "..", "protocol")
UNI = os.path.join(PROTO, "unified")

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-62s %s" % ("PASS" if cond else "FAIL", name, detail))


def load(p):
    with io.open(p, encoding="utf-8-sig") as f:
        return json.load(f)


def main() -> int:
    schema = load(os.path.join(UNI, "evidence-record.schema.json"))
    floor_doc = load(os.path.join(UNI, "evidence-floor.json"))

    # ---------- A-07：证据记录 schema ----------
    try:
        from jsonschema import Draft202012Validator

        Draft202012Validator.check_schema(schema)
        v = Draft202012Validator(schema)
        check("A-07 证据 schema 是合法 Draft 2020-12", True, "")
    except ImportError:
        v = None
        check("A-07 证据 schema 是合法 Draft 2020-12", False, "缺少 jsonschema 库")
    except Exception as e:
        v = None
        check("A-07 证据 schema 是合法 Draft 2020-12", False, str(e))

    if v is not None:
        base = {
            "evidence_id": "ev-00000001", "task_id": "task-00000001", "grade": "E1",
            "asserted_by": "dsh", "asserted_at": "2026-11-12T00:00:00Z",
            "command": "pytest -q", "exit_code": 0, "revision": "abc1234",
        }
        check("A-07 E1 合法样本通过", not list(v.iter_errors(base)), "")

        bad = dict(base); bad.pop("command")
        check("A-07 E1 缺 command 被拒", bool(list(v.iter_errors(bad))), "")

        e2 = {"evidence_id": "ev-00000002", "task_id": "task-00000002", "grade": "E2",
              "asserted_by": "dsh", "asserted_at": "2026-11-12T00:00:00Z", "peer": "tianshu"}
        check("A-07 E2 带 peer 通过", not list(v.iter_errors(e2)), "")
        e2bad = dict(e2); e2bad.pop("peer")
        check("A-07 E2 缺 peer 被拒", bool(list(v.iter_errors(e2bad))), "")

        e3 = {"evidence_id": "ev-00000003", "task_id": "task-00000003", "grade": "E3",
              "asserted_by": "x", "asserted_at": "2026-11-12T00:00:00Z", "note": "自证"}
        check("A-07 E3 带注记通过", not list(v.iter_errors(e3)), "")

        l3 = {"evidence_id": "ev-00000004", "task_id": "task-00000004", "grade": "E1",
              "asserted_by": "dsh", "asserted_at": "2026-11-12T00:00:00Z",
              "command": "pytest", "exit_code": 0, "revision": "r1",
              "rigor": "L3", "read_by": "tianshu",
              "payload": {"uri": "file:///r", "sha256": "a" * 64, "bytes": 3,
                          "media_type": "text/plain"}}
        check("A-07 L3 需 payload+read_by：齐全通过", not list(v.iter_errors(l3)), "")
        l3bad = dict(l3); l3bad.pop("read_by")
        check("A-07 L3 缺 read_by 被拒", bool(list(v.iter_errors(l3bad))), "")

        ext = dict(base); ext["cls"] = "external"; ext["grade"] = "E2"
        ext.pop("command"); ext["peer"] = "p"
        check("A-07 cls=external 必须 E1（E2 被拒）", bool(list(v.iter_errors(ext))), "")

    # ---------- C-01：floor 表与实现一致 ----------
    mismatches = []
    for rigor in ("L1", "L2", "L3"):
        for cls in ("readonly", "reversible", "external"):
            doc = floor_doc["axes"]["rigor"]["allowed_grades"][rigor]
            strength = floor_doc["grade_strength"]
            min_grade = floor_doc["axes"]["cls"]["min_grade"][cls]
            expect = sorted(g for g in doc if strength[g] >= strength[min_grade])
            got = sorted(U.floor(rigor, cls)["allowed_grades"])
            if expect != got:
                mismatches.append((rigor, cls, expect, got))
    check("C-01 floor 表 9 种组合与 unified.floor() 全部一致", not mismatches, str(mismatches[:2]))

    check("C-01 表内声明关闭 F-GAR-05",
          "F-GAR-05" in floor_doc.get("sources", {}).get("gap_closed", ""), "")
    check("C-01 审批要求与实现一致",
          all(floor_doc["axes"]["cls"]["approval_required"][c] == U.floor("L1", c)["approval_required"]
              for c in ("readonly", "reversible", "external")), "")

    # ---------- A-08：不保证清单 ----------
    ng_path = os.path.join(PROTO, "NOT-GUARANTEED.md")
    ng = io.open(ng_path, encoding="utf-8").read() if os.path.exists(ng_path) else ""
    for n in ("N1", "N2", "N3", "N4", "N5", "N6", "N7"):
        check("A-08 不保证清单含 %s" % n, n in ng, "")
    check("A-08 明写不保证 exactly-once", "exactly-once" in ng.lower(), "")
    check("A-08 明写单中枢约束", "单中枢" in ng or "多 Hub" in ng, "")
    check("A-08 明写审批身份为 asserted", "asserted" in ng, "")

    # ---------- D-02 / D-03：统一模型文档 ----------
    um = io.open(os.path.join(UNI, "unified-model.md"), encoding="utf-8").read()
    missing = [s for s in U.EXECUTION_STATES if s not in um]
    check("D-02 文档列全 8 个执行态", not missing, str(missing))
    check("D-02 文档列全 3 个治理态",
          all(s in um for s in U.GOVERNANCE_STATES), "")
    check("D-02 明写 SUCCEEDED 无裁定 → active", "无裁定" in um and "active" in um, "")
    check("D-03 明写 rigor 与 cls 正交", "正交" in um, "")
    check("D-03 明写只读的 L3 仍需 E1", "只读的 L3" in um, "")
    check("D-05 文档含责任矩阵", "责任矩阵" in um, "")
    check("D-04 文档声明证据=等级×载荷", "等级" in um and "载荷" in um, "")

    # ---------- 一致性：文档六值与实现 ----------
    for word in U.CANONICAL_VERDICTS:
        if word not in um:
            check("D-02 文档含裁定词 %s" % word, False, "")
            break
    else:
        check("D-02 文档含全部 6 个裁定词", True, "")

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
