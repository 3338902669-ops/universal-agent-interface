"""P2 自检：S8 账本哈希链 + B-04 附件分级 + E-06 故障注入格式。

S8 是威胁模型里登记的真实缺口（UAI 账本无链式完整性校验，GAR 有 verifyChain）。
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile

import ledger_chain as LC

HERE = os.path.dirname(os.path.abspath(__file__))
PROTO = os.path.join(HERE, "..", "protocol")

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-60s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="uai-chain-")
    led = os.path.join(tmp, "ledger.jsonl")

    # ---------- S8 基本封链 ----------
    a = LC.append_sealed(led, {"record": "TASK_STATE", "task_id": "t1", "state": "QUEUED"})
    b = LC.append_sealed(led, {"record": "TASK_STATE", "task_id": "t1", "state": "CLAIMED"})
    c = LC.append_sealed(led, {"record": "VERDICT", "task_id": "t1", "verdict": "通过"})
    check("S8 追加时自动封链", "self_hash" in a and "prev_hash" in a, "")
    check("S8 链式：第二条 prev 指向第一条 self", b["prev_hash"] == a["self_hash"], "")
    check("S8 链式：第三条 prev 指向第二条 self", c["prev_hash"] == b["self_hash"], "")

    v = LC.verify_chain(led)
    check("S8 完整链校验通过", v["ok"] is True and v["sealed"] == 3, json.dumps(v)[:80])
    head = LC.chain_head(led)
    check("S8 chain head 等于最后一条 self_hash", head == c["self_hash"], head[:16])

    # ---------- S8 篡改检测 ----------
    lines = io.open(led, encoding="utf-8").read().splitlines()
    rec = json.loads(lines[1])
    rec["state"] = "RUNNING"                  # 改内容，保留原 self_hash
    lines[1] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    io.open(led, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    v = LC.verify_chain(led)
    check("S8 改内容被检出", v["ok"] is False and (2, "self_hash_mismatch") in v["broken"],
          str(v["broken"]))

    # 恢复
    tmp2 = tempfile.mkdtemp(prefix="uai-chain2-")
    led2 = os.path.join(tmp2, "ledger.jsonl")
    LC.append_sealed(led2, {"record": "A", "n": 1})
    LC.append_sealed(led2, {"record": "B", "n": 2})
    LC.append_sealed(led2, {"record": "C", "n": 3})

    # 删中间一条
    ls = io.open(led2, encoding="utf-8").read().splitlines()
    io.open(led2, "w", encoding="utf-8").write("\n".join([ls[0], ls[2]]) + "\n")
    v = LC.verify_chain(led2)
    check("S8 删除中间记录被检出（prev 断链）",
          v["ok"] is False and any(r == "prev_hash_mismatch" for _, r in v["broken"]),
          str(v["broken"]))

    # 未链化的历史记录不算失败
    tmp3 = tempfile.mkdtemp(prefix="uai-chain3-")
    led3 = os.path.join(tmp3, "ledger.jsonl")
    io.open(led3, "w", encoding="utf-8").write(
        json.dumps({"record": "OLD", "n": 1}) + "\n" +
        json.dumps({"record": "OLD2", "n": 2}) + "\n")
    v = LC.verify_chain(led3)
    check("S8 未链化的历史记录不算失败（可渐进启用）",
          v["ok"] is True and v["unsealed"] == 2, json.dumps(v)[:80])

    # ---------- B-04 附件分级 ----------
    env = json.load(io.open(os.path.join(PROTO, "schemas", "envelope.schema.json"),
                            encoding="utf-8-sig"))
    att = env["properties"].get("attachments")
    check("B-04 envelope 有 attachments 定义", att is not None, "")
    item = (att or {}).get("items", {})
    check("B-04 attachments 项允许 classification 字段",
          "classification" in (item.get("properties") or {}), str(list((item.get("properties") or {}))))

    # ---------- E-06 故障注入格式 ----------
    fx = os.path.join(PROTO, "unified", "fault-injection.schema.json")
    check("E-06 故障注入 schema 存在", os.path.exists(fx), fx)
    if os.path.exists(fx):
        s = json.load(io.open(fx, encoding="utf-8-sig"))
        check("E-06 schema 要求 id/kind/expect/reference",
              all(k in s.get("required", []) for k in ("id", "kind", "expect")), str(s.get("required")))
        check("E-06 kind 枚举含六类故障",
              set(s["properties"]["kind"]["enum"]) >= {"loss", "duplicate", "stale_fence",
                                                        "false_complete", "timeout", "crash_window"},
              str(s["properties"]["kind"]["enum"]))

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
