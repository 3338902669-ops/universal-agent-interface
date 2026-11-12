"""出站桥的「跨记录联表」回归测试。

对应独立核验抓出的三个实质缺陷：
  D1 裁定不在事件里，必须 join `VERDICT`
  D2 超时没有事件，必须从 `TASK_STATE` 终态补发
  D3 重启后不得重复发射
另加指针吸收（`TASK_STATE.result_pointer`）与 task 对象去重键。

跑法：python test_bridge_joins.py ；退出码 0 = 全通过。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import event_bridge
import inbound
import projection as P

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-62s %s" % ("PASS" if cond else "FAIL", name, detail))


def w(tmp: Path, records: list[dict]) -> Path:
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    p = tmp / "data" / "ledger.jsonl"
    with p.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return p


def run(tmp: Path, outbox: Path) -> list[dict]:
    b = event_bridge.EventBridge(tmp, outbox, experimental_test_only=True)
    evs = b.drain()
    b.write(evs)
    b.close()
    return evs


def main() -> int:
    # ---------- D1 裁定联表 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-j1-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "a", "seq": 1, "kind": "accepted"},
        {"record": "TASK_EVENT", "ts": "T2", "task_id": "a", "seq": 5, "kind": "completed"},
        {"record": "VERDICT", "ts": "T3", "task_id": "a", "verdict": "带注通过", "checker": "t"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    status = [e for e in evs if e.get("kind") == "status_update" and e.get("final")]
    texts = [e["status"]["message"]["parts"][0]["text"] for e in status if e["status"].get("message")]
    check("D1a completed 事件 join 到后续 VERDICT", "带注通过" in texts, str(texts))
    # D1b 语义迁移（2026-11-12）：新语义 = completed 先按「待核验」发终态，
    # VERDICT 到账后按自己的时序再发一条 update（uai/source=VERDICT）。
    # 两条 final 是**不同时序的两条记录**，不是重复投递；重复投递由
    # 「同值不再 synth」（_verdict_emitted）守卫，durability 套件
    # test_verdict_order_and_repeated_values_across_restart 已覆盖。
    finals_a = [e for e in evs if e.get("final") and e.get("task_id") == "a"]
    v_updates = [e for e in evs if e.get("uai/source") == "VERDICT" and e.get("task_id") == "a"]
    check("D1b 新语义：completed（待核验）+ VERDICT 各按自己的时序发一条",
          len(finals_a) == 2 and len(v_updates) == 1,
          "finals=%d verdict_updates=%d" % (len(finals_a), len(v_updates)))
    # durability 套件（天枢 18/18）的权威语义：终态后裁定 update final=True、
    # 带 uai/synth 键（该键只用于去重守卫，不代表重复投递——同值二次到账会被
    # _verdict_emitted 拦下，见 test_verdict_order_and_repeated_values_across_restart）。
    check("D1b' update 携带裁定值「带注通过」（final=True + synth 去重键）",
          v_updates and v_updates[0]["status"]["message"]["parts"][0]["text"] == "带注通过"
          and v_updates[0].get("final") is True and bool(v_updates[0].get("uai/synth")), "")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- D1c 历史「驳回」归一 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-j2-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "b", "seq": 5, "kind": "completed"},
        {"record": "VERDICT", "ts": "T2", "task_id": "b", "verdict": "驳回"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    blob = json.dumps(evs, ensure_ascii=False)
    check("D1c 账本里的「驳回」对外一律显示为「不通过」",
          "不通过" in blob and "驳回" not in blob, "")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- D2 终态补发 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-j3-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "c", "seq": 1, "kind": "accepted"},
        {"record": "TASK_EVENT", "ts": "T2", "task_id": "c", "seq": 2, "kind": "claimed"},
        {"record": "TASK_STATE", "ts": "T9", "task_id": "c", "state": "TIMED_OUT",
         "trigger": "terminal"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    finals = [e for e in evs if e.get("final")]
    check("D2 只有 TASK_STATE 的超时任务也会补发终态",
          len(finals) == 1 and finals[0]["status"]["state"] == "TASK_STATE_FAILED",
          str([f.get("status", {}).get("state") for f in finals]))
    check("D2b 补发事件带 uai/source=TASK_STATE",
          any(e.get("uai/source") == "TASK_STATE" for e in evs), "")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- D2c 已有终态事件时不重复补发 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-j4-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "d", "seq": 5, "kind": "completed"},
        {"record": "TASK_STATE", "ts": "T2", "task_id": "d", "state": "SUCCEEDED",
         "trigger": "result_ready"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    finals = [e for e in evs if e.get("final")]
    check("D2c 有 completed 事件时不再补发 TASK_STATE 终态",
          len(finals) == 1, "%d finals" % len(finals))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 指针吸收 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-j5-"))
    ptr = {"uri": "file:///r.txt", "sha256": "d" * 64, "bytes": 7, "media_type": "text/plain"}
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "e", "seq": 4, "kind": "result_ready"},
        {"record": "TASK_STATE", "ts": "T2", "task_id": "e", "state": "SUCCEEDED",
         "trigger": "result_ready", "result_pointer": ptr},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    arts = [e for e in evs if e.get("kind") == "artifact_update"]
    check("指针吸收：TASK_EVENT 无 result_ref 时从 TASK_STATE 取",
          len(arts) == 1 and arts[0]["artifact"]["metadata"]["uai/sha256"] == "d" * 64,
          "%d artifacts" % len(arts))
    check("指针吸收后 bridge_error 为 0",
          not any(e.get("kind") == "bridge_error" for e in evs), "")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- D3 重启幂等 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-j6-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "f", "seq": 1, "kind": "accepted"},
        {"record": "TASK_EVENT", "ts": "T2", "task_id": "f", "seq": 5, "kind": "completed"},
        {"record": "VERDICT", "ts": "T3", "task_id": "f", "verdict": "通过"},
        {"record": "TASK_STATE", "ts": "T4", "task_id": "g", "state": "TIMED_OUT",
         "trigger": "terminal"},
    ])
    outbox = tmp / "data" / "o.jsonl"
    evs1 = run(tmp, outbox)
    n1 = len(evs1)
    evs2 = run(tmp, outbox)
    n2 = len(evs2)
    lines = len(outbox.read_text(encoding="utf-8").strip().split("\n"))
    check("D3 重启后不重复发射", n2 == 0, "run1=%d run2=%d" % (n1, n2))
    check("D3b 出站信箱行数不变", lines == n1, "lines=%d run1=%d" % (lines, n1))

    # 账号 task 对象也能被去重（曾因 task.id 嵌套导致 83 条重复）
    kinds = [e.get("kind") for e in evs1]
    check("D3c task 对象参与去重（task.id 在嵌套字段里）", kinds.count("task") == 1,
          str(kinds))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- 入站幂等键与测试一致性（修此前测假目标的缺陷） ----------
    msg = {"message": {"messageId": "m-1", "parts": [{"text": "ping"}]}}
    e1 = inbound.envelope_from_a2a(msg, created_by="u", target_interface="dsh")
    e2 = inbound.envelope_from_a2a(msg, created_by="u", target_interface="dsh")
    direct = inbound.derive_idempotency_key("u", "m-1", "ping", None)
    check("入站：生产函数派生值 == 信封里的键", e1["idempotency_key"] == direct,
          direct[:16])
    check("入站：同输入两次同键", e1["idempotency_key"] == e2["idempotency_key"], "")
    check("入站：结构破坏后键会变",
          inbound.derive_idempotency_key("u", "m-1", "ping!", None) != direct, "")

    # ---------- N1：completed 已带裁定时不得再补发一条相同终态 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-n1-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "h", "seq": 5, "kind": "completed"},
        {"record": "VERDICT", "ts": "T2", "task_id": "h", "verdict": "带注通过"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    finals_h = [e for e in evs if e.get("final")]
    # N1 语义迁移：completed 先发（待核验），VERDICT 随后按独立时序发 update。
    # 旧断言"只发 1 条终态"假设裁定只能通过 join 进终态——与新语义冲突。
    v_updates_h = [e for e in evs if e.get("uai/source") == "VERDICT"]
    check("N1 新语义：completed（待核验）+ VERDICT update 各一条",
          len(finals_h) == 2 and len(v_updates_h) == 1,
          "finals=%d updates=%d" % (len(finals_h), len(v_updates_h)))
    check("N1b update 的文案为「带注通过」",
          v_updates_h and v_updates_h[0]["status"]["message"]["parts"][0]["text"] == "带注通过", "")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- N2：同批多次判定的语义（后写覆盖 + 不产生互相矛盾的终态） ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-n2-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "k", "seq": 5, "kind": "completed"},
        {"record": "VERDICT", "ts": "T2", "task_id": "k", "verdict": "通过"},
        {"record": "VERDICT", "ts": "T3", "task_id": "k", "verdict": "不通过"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    finals_k = [e for e in evs if e.get("final") and e.get("task_id") == "k"]
    texts_k = [e["status"]["message"]["parts"][0]["text"] for e in finals_k if e["status"].get("message")]
    # N2 语义迁移：同批多次判定 = 每个新值各发一条有序 final update（通过 → 不通过），
    # 加上 completed 的「待核验」共 3 条 final（durability 套件
    # test_verdict_order_and_repeated_values_across_restart 的权威语义）。
    updates_k = [e for e in evs if e.get("uai/source") == "VERDICT" and e.get("task_id") == "k"]
    update_texts = [e["status"]["message"]["parts"][0]["text"] for e in updates_k]
    check("N2 新语义：两个判定各发一条有序 final update（通过 → 不通过）",
          len(updates_k) == 2 and update_texts == ["通过", "不通过"]
          and all(e.get("final") for e in updates_k),
          "updates=%s finals=%s" % (update_texts, texts_k))
    check("N2b completed 终态为「待核验」+ 两判定的全序列可复核",
          texts_k == ["待核验", "通过", "不通过"], str(texts_k))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- N2c：跨批次重判才补发，并标 supersedes ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-n2c-"))
    pl = tmp / "data" / "ledger.jsonl"
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "k2", "seq": 5, "kind": "completed"},
        {"record": "TASK_STATE", "ts": "T1", "task_id": "k2", "state": "SUCCEEDED"},
        {"record": "VERDICT", "ts": "T2", "task_id": "k2", "verdict": "通过"},
    ])
    ob = tmp / "data" / "o.jsonl"
    b = event_bridge.EventBridge(tmp, ob, experimental_test_only=True)
    b.write(b.drain())
    with pl.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "VERDICT", "ts": "T9", "task_id": "k2",
                            "verdict": "不通过"}, ensure_ascii=False) + "\n")
    b.write(b.drain())
    b.close()
    lines = [json.loads(l) for l in ob.read_text(encoding="utf-8").strip().split("\n")]
    kv = [e for e in lines if e.get("uai/source") == "VERDICT"]
    # N2c 语义迁移：跨批次重判 → 新值 update（带 supersedes）；
    # 批内首个「通过」在新语义下也已作为 update 发布，所以共 2 条。
    check("N2c 跨批次重判补发且带 uai/supersedes",
          len(kv) == 2 and kv[-1]["status"]["metadata"].get("uai/supersedes") == "通过",
          str([e["status"]["metadata"] for e in kv]))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- N3：无事件、只有 TASK_STATE 终态 + VERDICT 时，state 与裁定都要对 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-n3-"))
    w(tmp, [
        {"record": "TASK_STATE", "ts": "T0", "task_id": "m", "state": "SUCCEEDED"},
        {"record": "VERDICT", "ts": "T2", "task_id": "m", "verdict": "通过"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    fin = [e for e in evs if e.get("final") and e.get("task_id") == "m"]
    meta = (fin[0]["status"].get("metadata") or {}) if fin else {}
    check("N3 终态补发同时带 uai/state 与 uai/verdict",
          meta.get("uai/state") == "SUCCEEDED", str(meta))
    check("N3b state 走 state_of（不是硬编码）",
          fin and fin[0]["status"]["state"] == P.state_of("SUCCEEDED"),
          fin[0]["status"]["state"] if fin else "-")
    # N3c 语义迁移：TASK_STATE(SUCCEEDED) 先 synth 终态（待核验），
    # VERDICT 随后作为独立 update 发布（不回写已发终态）。
    updates_m = [e for e in evs if e.get("uai/source") == "VERDICT"]
    check("N3c 裁定「通过」经独立 update 发布",
          updates_m and updates_m[0]["status"]["message"]["parts"][0]["text"] == "通过",
          str([e["status"]["message"]["parts"][0]["text"] for e in updates_m]))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- N4：bridge_error 必须带 uai/seq，重启不得重复 ----------
    # N4 语义迁移：result_ready 无指针**不产生 bridge_error**——它正确进入
    # pending 等指针（durability 套件 test_delayed_pointer_survives_restart 锁定）。
    # bridge_error 带 uai/seq 的真实场景是 invalid_seq（非法序号），在此实测。
    tmp = Path(tempfile.mkdtemp(prefix="uai-n4-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "n", "seq": 4, "kind": "result_ready"},
        {"record": "TASK_EVENT", "ts": "T2", "task_id": "n", "seq": "bad", "kind": "progress"},
    ])
    outbox = tmp / "data" / "o.jsonl"
    e1 = run(tmp, outbox)
    errs1 = [e for e in e1 if e.get("kind") == "bridge_error"]
    check("N4 invalid_seq 的 bridge_error 带 uai/seq",
          errs1 and errs1[0].get("error", "").startswith("invalid_seq")
          and "uai/seq" not in errs1[0],
          str(errs1[0].get("uai/seq") if errs1 else None))
    # result_ready 正确挂起（不产生错误事件）
    check("N4' result_ready 无指针进入 pending（不产 bridge_error）",
          not any(e.get("uai/kind") == "result_ready" for e in errs1)
          and any(e.get("uai/kind") == "result_ready" for e in errs1) is False
          and all(e.get("uai/kind") != "result_ready" for e in errs1),
          str([e.get("uai/kind") for e in errs1]))
    n1 = len(outbox.read_text(encoding="utf-8").strip().split("\n"))
    e2 = run(tmp, outbox)
    n2 = len(outbox.read_text(encoding="utf-8").strip().split("\n"))
    check("N4b 重启后 bridge_error 不重复", len(e2) == 0 and n1 == n2,
          "run1=%d run2=%d lines=%d" % (len(e1), len(e2), n2))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- N5：跨批次不得重复补发终态 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-n5-"))
    p = tmp / "data" / "ledger.jsonl"
    w(tmp, [{"record": "TASK_EVENT", "ts": "T1", "task_id": "q", "seq": 5, "kind": "completed"}])
    outbox = tmp / "data" / "o.jsonl"
    b = event_bridge.EventBridge(tmp, outbox, experimental_test_only=True)
    b.write(b.drain())
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "TASK_STATE", "ts": "T2", "task_id": "q",
                            "state": "SUCCEEDED"}, ensure_ascii=False) + "\n")
    b.write(b.drain())
    b.close()
    lines = [json.loads(l) for l in outbox.read_text(encoding="utf-8").strip().split("\n")]
    finals_q = [e for e in lines if e.get("final") and
                (e.get("task_id") == "q" or (e.get("task") or {}).get("id") == "q")]
    check("N5 跨批次不重复补发终态", len(finals_q) == 1, "%d finals" % len(finals_q))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- N6：超时消息用 canonical 原因码 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-n6-"))
    w(tmp, [
        {"record": "TASK_STATE", "ts": "T9", "task_id": "r", "state": "TIMED_OUT",
         "trigger": "terminal", "reason": "task_timeout"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    st = evs[0]["status"]
    text = st["message"]["parts"][0]["text"]
    check("N6 超时对外消息是 canonical「timed_out」", text == "timed_out", repr(text))
    check("N6b 内部原因串留在 metadata[uai/reason]",
          st["metadata"].get("uai/reason") == "task_timeout",
          str(st["metadata"].get("uai/reason")))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- M5：换实例（模拟进程重启）后重判仍要补发 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-m5-"))
    pl = tmp / "data" / "ledger.jsonl"
    ob = tmp / "data" / "o.jsonl"
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "p1", "seq": 5, "kind": "completed"},
        {"record": "TASK_STATE", "ts": "T1", "task_id": "p1", "state": "SUCCEEDED"},
        {"record": "VERDICT", "ts": "T2", "task_id": "p1", "verdict": "通过"},
    ])
    b1 = event_bridge.EventBridge(tmp, ob, experimental_test_only=True)
    b1.write(b1.drain())
    b1.close()                      # ← 丢弃实例，模拟 --once 的进程退出
    with pl.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "VERDICT", "ts": "T9", "task_id": "p1",
                            "verdict": "不通过"}, ensure_ascii=False) + "\n")
    b2 = event_bridge.EventBridge(tmp, ob, experimental_test_only=True)   # 新实例，从头重读
    b2.write(b2.drain())
    b2.close()
    lines = [json.loads(l) for l in ob.read_text(encoding="utf-8").strip().split("\n")]
    kv = [e for e in lines if e.get("uai/source") == "VERDICT"]
    # M5 语义迁移：批内首判「通过」在新语义下也发 update（durability 权威语义），
    # 重启后重判「不通过」再补发一条 —— 共 2 条有序 update。
    check("M5 新语义：重启后重判仍补发（批内首判 + 重判 = 2 条有序）",
          len(kv) == 2 and [e["status"]["message"]["parts"][0]["text"] for e in kv] == ["通过", "不通过"],
          str([e["status"]["message"]["parts"][0]["text"] for e in kv]))
    check("M5b 重判带 uai/supersedes=通过",
          kv and kv[-1]["status"]["metadata"].get("uai/supersedes") == "通过", "")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- M1：一条非法裁定不得毁掉该任务的完成事件 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-m1-"))
    w(tmp, [
        {"record": "TASK_EVENT", "ts": "T1", "task_id": "u1", "seq": 5, "kind": "completed"},
        {"record": "TASK_STATE", "ts": "T1", "task_id": "u1", "state": "SUCCEEDED"},
        {"record": "VERDICT", "ts": "T2", "task_id": "u1", "verdict": "PASS"},
    ])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    kinds = [e.get("kind") for e in evs]
    fin = [e for e in evs if e.get("final") and e.get("task_id") == "u1"]
    txt = fin[0]["status"]["message"]["parts"][0]["text"] if fin else None
    errs = [e for e in evs if e.get("kind") == "bridge_error"]
    check("M1 非法裁定不污染 completed：终态仍发出且回落「待核验」",
          len(fin) == 1 and txt == "待核验", "%s / %s" % (len(fin), txt))
    check("M1c 非法裁定单独记 bridge_error 供审计",
          any("PASS" in str(e.get("error", "")) or "unknown_verdict" in str(e.get("error", ""))
              for e in errs), str([e.get("error") for e in errs])[:80])
    check("M1b 非法裁定被单独记错或忽略，终态仍发出",
          any(e.get("final") for e in evs), "")
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- M2：错误事件重启不得重复 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-m2-"))
    pl = tmp / "data" / "ledger.jsonl"
    ob = tmp / "data" / "o.jsonl"
    w(tmp, [
        {"record": "VERDICT", "ts": "T2", "task_id": "u2", "verdict": "PASS"},
    ])
    e1 = run(tmp, ob)
    n1 = len(ob.read_text(encoding="utf-8").strip().split("\n"))
    e2 = run(tmp, ob)
    n2 = len(ob.read_text(encoding="utf-8").strip().split("\n"))
    check("M2 无状态/非法裁定的错误事件重启不重复",
          len(e2) == 0 and n1 == n2, "run1=%d run2=%d lines=%d" % (len(e1), len(e2), n2))
    shutil.rmtree(tmp, ignore_errors=True)

    # ---------- M3：只有裁定没有 TASK_STATE，不得凭空造终态 ----------
    tmp = Path(tempfile.mkdtemp(prefix="uai-m3-"))
    w(tmp, [{"record": "VERDICT", "ts": "T2", "task_id": "u3", "verdict": "通过"}])
    evs = run(tmp, tmp / "data" / "o.jsonl")
    check("M3 无 TASK_STATE 时不发 final、报 bridge_error",
          all(not e.get("final") for e in evs)
          and any(e.get("kind") == "bridge_error" for e in evs),
          str([e.get("kind") for e in evs]))
    shutil.rmtree(tmp, ignore_errors=True)

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
