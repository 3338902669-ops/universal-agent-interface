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

    # ---------- B-04 附件分级（按行为校验，不按 schema 形状断言） ----------
    # 旧断言只检查 items.properties 里有没有 classification，这恰恰放过了真实缺陷：
    # classification 写在 $ref 兄弟位，而 $defs.artifactPointer 是 additionalProperties:false，
    # 于是「带分级的附件」被校验器整体拒收 —— 字段存在但从未可用。
    env_path = os.path.join(PROTO, "schemas", "envelope.schema.json")
    env = json.load(io.open(env_path, encoding="utf-8-sig"))
    try:
        import jsonschema
        from jsonschema import Draft202012Validator
        Draft202012Validator.check_schema(env)
        have_js = True
    except Exception as e:                                     # noqa: BLE001
        have_js = False
        check("B-04 jsonschema 可用", False, str(e)[:80])

    if have_js:
        base = json.load(io.open(os.path.join(PROTO, "tests", "conformance", "fixtures",
                                              "envelopes", "readonly.json"),
                                 encoding="utf-8-sig"))

        def accepts(mut: dict) -> bool:
            e = json.loads(json.dumps(base))
            mut(e)
            return Draft202012Validator(env).is_valid(e)

        ptr = {"uri": "file:///tmp/a.txt", "sha256": "a" * 64, "bytes": 3, "media_type": "text/plain"}
        check("B-04 带 classification 的附件可被接受",
              accepts(lambda e: e.update(attachments=[dict(ptr, classification="confidential")])), "")
        check("B-04 非法 classification 被拒",
              not accepts(lambda e: e.update(attachments=[dict(ptr, classification="bogus")])), "")
        check("B-04 缺 classification 仍合法（可选字段）",
              accepts(lambda e: e.update(attachments=[dict(ptr)])), "")
        check("B-04 附件仍不许未知字段",
              not accepts(lambda e: e.update(attachments=[dict(ptr, secret="x")])), "")

        # ---------- B-03 SLA / 结算 / 驻留 ----------
        check("B-03 sla 可声明", accepts(lambda e: e.update(sla={"response_within_s": 60, "max_attempts": 3,
                                                               "deadline_at": "2026-11-13T00:00:00Z"})), "")
        check("B-03 sla.max_attempts 不得为 0",
              not accepts(lambda e: e.update(sla={"max_attempts": 0})), "")
        check("B-03 billing 允许 unit_price=null（未定价≠0）",
              accepts(lambda e: e.update(billing={"basis": "per_task", "unit_price": None, "currency": "CNY"})), "")
        check("B-03 billing 未知 basis 被拒",
              not accepts(lambda e: e.update(billing={"basis": "per_hour"})), "")
        check("B-03 billing currency 必为大写三字母",
              not accepts(lambda e: e.update(billing={"basis": "per_task", "currency": "cny"}))
              and accepts(lambda e: e.update(billing={"basis": "per_task", "currency": "CNY"})), "")
        check("B-03 residency 可声明",
              accepts(lambda e: e.update(residency={"store_in": ["CN"], "may_leave": False,
                                                    "retention_days": 0})), "")
        check("B-03 residency 区域非 ISO 被拒",
              not accepts(lambda e: e.update(residency={"store_in": ["china"]})), "")
        check("B-03 residency may_leave 默认 false（schema default）",
              env["$defs"]["residency"]["properties"]["may_leave"].get("default") is False, "")
        check("B-03 三个扩展块都声明为可选（不在 required）",
              all(k not in env["required"] for k in ("sla", "billing", "residency")), str(env["required"][:3]) + "...")

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
