"""跨仓词表一致性检查（关掉审计缺口 D-01）。

读三仓真实文件里的枚举，与 `unified.py` / `protocol/unified/*.json` 的唯一事实源比对。
任一侧漂移 → 退出码 1。源文件缺失 → 记 SKIP，不算失败（便于在 CI 单仓克隆时运行）。

跑法：python check-cross-repo-vocab.py [--strict]
      --strict 时 SKIP 也视为失败。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

import unified as U

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
HOME = os.path.expanduser("~")
PROTO = os.path.join(REPO, "protocol")
UNI = os.path.join(PROTO, "unified")

# 兄弟仓与官方规格：CI 单仓克隆时不存在，允许 SKIP
SIBLING = {
    "ao_conformance": os.path.join(HOME, "workspace", "projects", "joint-audit",
                                   "agent-orchestra", "conformance.json"),
    "gar_findings": os.path.join(HOME, "workspace", "projects", "joint-audit",
                                 "governed-agent-runtime", "KNOWN-FINDINGS.md"),
}
A2A_PROTO = os.path.join(HOME, "workspace", "projects", "a2a-official-spec", "a2a.proto")

STRICT = "--strict" in sys.argv
FAILS, SKIPS = [], []


def ok(name, cond, detail=""):
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


def skip(name, why):
    print("%s  %-58s %s" % ("SKIP", name, why))
    SKIPS.append(name)


def read(p):
    with io.open(p, encoding="utf-8-sig") as f:
        return f.read()


def need(p):
    if not os.path.exists(p):
        return None
    return p


def main() -> int:
    # ---------- 1. UAI 契约正文八态 ----------
    contract = need(os.path.join(PROTO, "contract-v1.md"))
    if contract:
        found = set(re.findall(
            r"\b(QUEUED|DISPATCHED|CLAIMED|RUNNING|SUCCEEDED|FAILED|TIMED_OUT|CANCELLED)\b",
            read(contract)))
        ok("UAI contract-v1 八态与 unified 一致", found == set(U.EXECUTION_STATES),
           "差异=%s" % (found ^ set(U.EXECUTION_STATES)))
    else:
        skip("UAI contract-v1 八态", "文件缺失")

    # ---------- 2. verdict schema 六值 ----------
    vs_path = need(os.path.join(PROTO, "schemas", "verdict.schema.json"))
    if vs_path:
        enum = tuple(json.loads(read(vs_path))["properties"]["verdict"]["enum"])
        ok("UAI verdict.schema 六值与 unified 逐字一致", enum == U.CANONICAL_VERDICTS, str(enum))
    else:
        skip("verdict schema 六值", "文件缺失")

    # ---------- 3. 别名表 ----------
    va_path = need(os.path.join(PROTO, "schemas", "verdict-aliases.json"))
    if va_path:
        va = json.loads(read(va_path))
        alias_map = {a["historical_value"]: a["canonical_value"] for a in va["aliases"]}
        ok("别名表与 LEGACY_ALIASES 一致", alias_map == U.LEGACY_ALIASES, str(alias_map))
        ok("别名表禁止写入与输出",
           va["constraints"]["allow_on_write"] is False
           and va["constraints"]["allow_in_output"] is False, "")
    else:
        skip("别名表", "文件缺失")

    # ---------- 4. 新增：evidence-floor 与 evidence schema ----------
    floor_path = need(os.path.join(UNI, "evidence-floor.json"))
    if floor_path:
        fd = json.loads(read(floor_path))
        strength = fd["grade_strength"]
        bad = []
        for rigor, grades in fd["axes"]["rigor"]["allowed_grades"].items():
            for cls, min_grade in fd["axes"]["cls"]["min_grade"].items():
                expect = sorted(g for g in grades if strength[g] >= strength[min_grade])
                if expect != sorted(U.floor(rigor, cls)["allowed_grades"]):
                    bad.append((rigor, cls))
        ok("evidence-floor 9 组合与 unified.floor() 一致", not bad, str(bad[:2]))
    else:
        skip("evidence-floor", "文件缺失")

    ev_path = need(os.path.join(UNI, "evidence-record.schema.json"))
    if ev_path:
        s = json.loads(read(ev_path))
        ok("evidence schema 的 grade 枚举与 unified 一致",
           tuple(s["properties"]["grade"]["enum"]) == U.GRADES, str(s["properties"]["grade"]["enum"]))
        ok("evidence schema 声明 rigor/cls 轴",
           "rigor" in s["properties"] and "cls" in s["properties"], "")
    else:
        skip("evidence schema", "文件缺失")

    # ---------- 5. A2A 官方枚举 ----------
    if need(A2A_PROTO):
        proto = read(A2A_PROTO)
        m = re.search(r"enum\s+TaskState\s*\{(.*?)\}", proto, re.S)
        official = set(re.findall(r"(TASK_STATE_[A-Z_]+)\s*=", m.group(1))) if m else set()
        ok("A2A 官方枚举解析出 9 值", len(official) == 9, "%d" % len(official))
        used = set(U.EXECUTION_TO_A2A.values())
        ok("A2A 像集全部属于官方枚举", used <= official, "越界=%s" % (used - official))
        ok("没有把任何执行态映成 REJECTED", "TASK_STATE_REJECTED" not in used, "")
    else:
        skip("A2A 官方枚举", "a2a.proto 缺失")

    # ---------- 6. AO conformance ----------
    if need(SIBLING["ao_conformance"]):
        reqs = json.loads(read(SIBLING["ao_conformance"]))["requirements"]
        by_id = {r["id"]: r for r in reqs}
        blob = " ".join(r["requirement"] for r in reqs)
        grades = set(re.findall(r"\bE[1-4]\b", blob))
        ok("AO 证据等级与 unified 一致", grades == set(U.GRADES), str(sorted(grades)))
        rigor = set(re.findall(r"\bL[1-3]\b", blob))
        ok("AO 严格度与 unified 一致", rigor == set(U.RIGOR_GRADE_FLOOR), str(sorted(rigor)))
        ok("AO 的 A4 仍为 OUT OF SCOPE", by_id["A4"]["status"] == "OUT OF SCOPE",
           by_id["A4"]["status"])
        ok("AO 的 E6 仍为 OUT OF SCOPE", by_id["E6"]["status"] == "OUT OF SCOPE",
           by_id["E6"]["status"])
    else:
        skip("AO conformance", "兄弟仓未克隆")

    # ---------- 7. GAR findings ----------
    if need(SIBLING["gar_findings"]):
        gar = read(SIBLING["gar_findings"])
        for fid in ("F-GAR-02", "F-GAR-05", "F-GAR-08"):
            ok("GAR 的 %s 仍登记在册" % fid, fid in gar, "")
    else:
        skip("GAR findings", "兄弟仓未克隆")

    print()
    if FAILS:
        print("结果：%d 项不一致" % len(FAILS))
        for f in FAILS:
            print("  ! " + f)
        return 1
    if SKIPS and STRICT:
        print("结果：0 项漂移，但有 %d 项 SKIP（--strict 下视为失败）" % len(SKIPS))
        return 1
    print("结果：0 项漂移（SKIP %d 项）" % len(SKIPS))
    return 0


if __name__ == "__main__":
    sys.exit(main())
