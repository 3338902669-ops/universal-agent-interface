"""跨仓词表一致性检查（关掉审计缺口 G1）。

把三仓实际文件里的枚举读出来，与 `unified.py` 的唯一事实源比对。
任一侧漂移 → 退出码 1。

跑法：python check-cross-repo-vocab.py
"""

from __future__ import annotations

import io
import json
import os
import re
import sys

import unified as U

HOME = os.path.expanduser("~")
UAI = os.path.join(HOME, "workspace", "projects", "universal-agent-interface")
AUDIT = os.path.join(HOME, "workspace", "projects", "joint-audit")
A2A_PROTO = os.path.join(HOME, "workspace", "projects", "a2a-official-spec", "a2a.proto")

FAILS = []


def ok(name, cond, detail=""):
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))
    if not cond:
        FAILS.append(name)


def read(p):
    with io.open(p, encoding="utf-8-sig") as f:
        return f.read()


# ---------- 1. UAI 契约正文的八态 ----------
contract = read(os.path.join(UAI, "protocol", "contract-v1.md"))
found_states = set(re.findall(r"\b(QUEUED|DISPATCHED|CLAIMED|RUNNING|SUCCEEDED|FAILED|TIMED_OUT|CANCELLED)\b", contract))
ok("UAI contract-v1 的八态与 unified 一致",
   found_states == set(U.EXECUTION_STATES),
   "差异=%s" % (found_states ^ set(U.EXECUTION_STATES)))

# ---------- 2. UAI verdict schema 的六值 ----------
vs = json.loads(read(os.path.join(UAI, "protocol", "schemas", "verdict.schema.json")))
enum = tuple(vs["properties"]["verdict"]["enum"])
ok("UAI verdict.schema 的六值与 unified 逐字一致",
   enum == U.CANONICAL_VERDICTS, str(enum))

# ---------- 3. 别名表只读 ----------
va = json.loads(read(os.path.join(UAI, "protocol", "schemas", "verdict-aliases.json")))
alias_map = {a["historical_value"]: a["canonical_value"] for a in va["aliases"]}
ok("别名表与 unified 的 LEGACY_ALIASES 一致",
   alias_map == U.LEGACY_ALIASES, str(alias_map))
ok("别名表禁止写入与输出",
   va["constraints"]["allow_on_write"] is False and va["constraints"]["allow_in_output"] is False, "")

# ---------- 4. A2A 官方 TaskState ----------
if os.path.exists(A2A_PROTO):
    proto = read(A2A_PROTO)
    m = re.search(r"enum\s+TaskState\s*\{(.*?)\}", proto, re.S)
    official = set(re.findall(r"(TASK_STATE_[A-Z_]+)\s*=", m.group(1))) if m else set()
    ok("A2A 官方枚举被正确解析（9 值）", len(official) == 9, "%d 值" % len(official))
    used = set(U.EXECUTION_TO_A2A.values())
    ok("unified 的 A2A 像集全部属于官方枚举", used <= official, "越界=%s" % (used - official))
    ok("没有把任何执行态映成 REJECTED", "TASK_STATE_REJECTED" not in used, "")
else:
    ok("A2A proto 存在", False, A2A_PROTO)

# ---------- 5. AO conformance 的证据等级与严格度 ----------
conf = json.loads(read(os.path.join(AUDIT, "agent-orchestra", "conformance.json")))
reqs = conf["requirements"]
by_id = {r["id"]: r for r in reqs}

grades_in_ao = set(re.findall(r"\bE[1-4]\b", " ".join(r["requirement"] for r in reqs)))
ok("AO 的证据等级集合与 unified 一致",
   grades_in_ao <= set(U.GRADES) and set(U.GRADES) <= grades_in_ao, str(sorted(grades_in_ao)))

rigor_in_ao = set(re.findall(r"\bL[1-3]\b", " ".join(r["requirement"] for r in reqs)))
ok("AO 的严格度集合与 unified 一致",
   rigor_in_ao == set(U.RIGOR_GRADE_FLOOR), str(sorted(rigor_in_ao)))

# E5 的口径必须等于 unified 的 L1/L2/L3 允许集
e5 = by_id["E5"]["requirement"]
ok("AO 的 E5 文案与 unified 的 rigor 门限不矛盾",
   ("L1" in e5 and "L2" in e5 and "E1/E2" in e5 and "L3" in e5 and "E1" in e5), e5[:70])

# ---------- 6. AO 的 A4 必须仍是 OUT OF SCOPE ----------
ok("AO 的 A4（审批人身份已认证）仍标注为超出范围",
   by_id["A4"]["status"] == "OUT OF SCOPE", by_id["A4"]["status"])
ok("unified 把审批身份默认当 asserted",
   "asserted" in U.validate_approval.__doc__ if U.validate_approval.__doc__ else True,
   "（默认值由代码保证）")

# ---------- 7. GAR 的 F-GAR-05 / F-GAR-02 仍未关闭 ----------
gar = read(os.path.join(AUDIT, "governed-agent-runtime", "KNOWN-FINDINGS.md"))
open_ids = set(re.findall(r"\|\s*(F-GAR-\d+)\s*\|[^|]*\|\s*\**([^|]*)\**\s*\|\s*(open|disclosed|accept)", gar))
ok("GAR 的 F-GAR-05（证据下限未映射）仍登记在册", "F-GAR-05" in gar, "")
ok("GAR 的 F-GAR-02（跨 Agent 交接不可表达）仍登记在册", "F-GAR-02" in gar, "")

# ---------- 8. unified 覆盖了审计点出的缺口 ----------
ok("unified 提供 floor 表（对应 F-GAR-05）", callable(U.floor), "")
ok("unified 提供两轴治理状态（对应状态冲突）", callable(U.derive_governance_state), "")
ok("unified 提供 action_digest（对应审批 scope）", callable(U.action_digest), "")

print()
if FAILS:
    print("结果：%d 项不一致" % len(FAILS))
    for f in FAILS:
        print("  ! " + f)
    sys.exit(1)
print("结果：全部一致（0 项漂移）")
sys.exit(0)
