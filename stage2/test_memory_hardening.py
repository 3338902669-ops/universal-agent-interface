"""P5 加固自检：独立审查抓到的 5 类 P1 缺陷的回归测试。

对应缺陷（reviewer 2026-11-20）：
  1. 凭据扫描只覆盖 3 个字段，refs/session 可绕过；标记表过窄且会误杀。
  2. outbox 并发 claim 可重复领取；mark_failed 能把 delivered 改回 retry。
  3. 兜底写入自证 verified，审计把「未确认」算成「有记忆」。
  4. 终态记忆异常被静默吞掉，账本无痕；裁定路径会等图谱。
  5. 新旧图属性名分裂（uai_intent_key vs uai_key）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

os.environ["UAI_GRAPH_OUTCOMES"] = "0"

import memory_audit as MA          # noqa: E402
import memory_intent as MI         # noqa: E402
import memory_outbox as MO         # noqa: E402
import memory_pipeline             # noqa: E402
import memory_writer as MW         # noqa: E402
from hub import Hub, Task          # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def base(**over):
    value = {
        "intent_id": "int-h1",
        "kind": "decision",
        "subject": "加固",
        "claim": "内存管道加固",
        "evidence_level": "E1",
        "evidence": "cmd exit=0",
        "group_id": "team",
        "reference_time": "2026-11-20T01:59:51+08:00",
    }
    value.update(over)
    return value


# ---------- 1. 凭据 ----------

def test_credential_fields():
    r = MI.validate_intent(base(refs={"db": "password: hunter2"}))
    check("H-1 refs 里的凭据被拒", r == "invalid_intent:credential_like_text", str(r))
    r = MI.validate_intent(base(source_session="token is abc123"))
    check("H-2 source_session 里的凭据被拒", r == "invalid_intent:credential_like_text", str(r))
    r = MI.validate_intent(base(task_id="AKIAIOSFODNN7EXAMPLE"))
    check("H-3 task_id 里的云凭据被拒", r == "invalid_intent:credential_like_text", str(r))


def test_credential_phrasing():
    r = MI.validate_intent(base(claim="my token is abc123"))
    check("H-4 自然语句里的 token 被拒", r == "invalid_intent:credential_like_text", str(r))
    r = MI.validate_intent(base(claim="api key: sk-live-9f2"))
    check("H-5 api key: 形式被拒", r == "invalid_intent:credential_like_text", str(r))
    r = MI.validate_intent(base(claim="private key -----BEGIN RSA PRIVATE KEY-----"))
    check("H-6 私钥块被拒", r == "invalid_intent:credential_like_text", str(r))
    r = MI.validate_intent(base(claim="这次是 secret sauce 的调参思路"))
    check("H-7 secret 普通用法不误杀", r is None, str(r))


def test_payload_never_holds_secret():
    db = Path(tempfile.mkdtemp()) / "o.db"
    ob = MO.MemoryOutbox(db)
    out = ob.enqueue(base(intent_id="int-h-secret", refs={"k": "password: hunter2"}))
    check("H-8 含凭据的 intent 不入队", out["enqueued"] is False, str(out))
    raw = db.read_bytes() if db.exists() else b""
    check("H-9 磁盘上找不到凭据", b"hunter2" not in raw, "bytes=%d" % len(raw))


# ---------- 2. outbox 并发与状态 ----------

def test_concurrent_claim():
    db = Path(tempfile.mkdtemp()) / "c.db"
    ob = MO.MemoryOutbox(db)
    total = 60
    for i in range(total):
        ob.enqueue(base(intent_id="p-%03d" % i))
    got = []
    lock = threading.Lock()

    def worker():
        local = []
        for _ in range(5):
            for row in ob.claim_due(limit=total):
                local.append(row["intent_id"])
        with lock:
            got.extend(local)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("H-10 并发不重复领取", len(got) == len(set(got)), "claims=%d unique=%d"
          % (len(got), len(set(got))))
    check("H-11 领取数不超过总数", len(set(got)) <= total, str(len(set(got))))


def test_mark_failed_after_delivered():
    db = Path(tempfile.mkdtemp()) / "s.db"
    ob = MO.MemoryOutbox(db)
    ob.enqueue(base(intent_id="int-h2"))
    ob.claim_due()
    ob.mark_delivered("int-h2", verified="verified", write_path="graphiti")
    state = ob.mark_failed("int-h2", "late_error")
    row = ob.get("int-h2")
    check("H-12 已投递不被 mark_failed 回滚", row["status"] == "delivered", row["status"])
    check("H-13 回滚请求被明确忽略", state == "ignored", str(state))


# ---------- 3. 回读与审计 ----------

def test_verify_honesty():
    payload = {"intent_key": "uai|int-h3", "name": "uai:int-h3", "group_id": "team",
               "body": "x", "reference_time": "2026-11-20T01:59:51+08:00",
               "source_description": "uai-memory/test"}
    result = MW._verify(payload, "neo4j")
    check("H-14 兜底写入不自证 verified", result is None, str(result))


def test_audit_unverified_gap():
    db = Path(tempfile.mkdtemp()) / "a.db"
    ob = MO.MemoryOutbox(db)
    ob.enqueue(base(intent_id="int-h4", kind="verdict", task_id="task-h4"))
    ob.claim_due()
    ob.mark_delivered("int-h4", verified="unverified", write_path="neo4j", degraded=True)
    ledger = Path(tempfile.mkdtemp()) / "ledger.jsonl"
    ledger.write_text(json.dumps({"record": "TASK_STATE", "ts": "2026-11-20T01:00:00Z",
                                  "task_id": "task-h4", "state": "SUCCEEDED"},
                                 ensure_ascii=False) + "\n", encoding="utf-8")
    audit = MA.build_audit(ob, ledger_path=ledger)
    check("H-15 未确认写入不算覆盖", audit["coverage"]["with_memory"] == 0,
          str(audit["coverage"]))
    reasons = [row["reason"] for row in audit["unrecorded"]]
    check("H-16 未确认写入被点名", reasons == ["unverified_write"], str(reasons))
    check("H-17 审计单列未确认写入", len(audit.get("unverified", [])) == 1,
          str(len(audit.get("unverified", []))))


# ---------- 4. hub 不阻塞 / 不静默 ----------

def make_hub():
    base_dir = Path(tempfile.mkdtemp())
    (base_dir / "data").mkdir(parents=True, exist_ok=True)
    hub = Hub(base_dir, "nats://127.0.0.1:43321")
    hub.memory_drain_s = 0
    env = {"task_id": "t-hub-h", "trace_id": "tr", "idempotency_key": "idem",
           "created_by": "test", "instruction": "hub hardening", "params": {},
           "timeout_s": 60}
    task = Task(task_id="t-hub-h", envelope=env, trace_id="tr",
                idempotency_key="idem", state="SUCCEEDED")
    task.selected_interface = "codex"
    hub.tasks["t-hub-h"] = task
    return hub, task


class Msg:
    def __init__(self, payload):
        self.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.reply = "r"
        self.responses = []

    async def respond(self, data):
        self.responses.append(json.loads(data.decode("utf-8")))


def test_verdict_does_not_block():
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"
    hub, _ = make_hub()
    original = memory_pipeline.record_verdict
    seen = {}

    def enqueue_only(**kwargs):
        # 真实契约：hub 只要求入队（flush=False），投递交给后台 drain
        seen.update(kwargs)
        return {"enqueued": True, "written": False, "status": "pending",
                "intent_id": "int-h5", "reason": "deferred"}

    memory_pipeline.record_verdict = enqueue_only
    try:
        start = time.time()
        msg = Msg({"task_id": "t-hub-h", "verdict": "通过", "checker": "tester"})
        asyncio.run(hub.on_verdict(msg))
        elapsed = time.time() - start
    finally:
        memory_pipeline.record_verdict = original
        os.environ["UAI_GRAPH_OUTCOMES"] = "0"
    gm = (msg.responses[-1] or {}).get("graph_memory") or {}
    check("H-18 裁定不等图谱 I/O（<1s）", elapsed < 1.0, "%.2fs" % elapsed)
    check("H-19 只要求入队，不在裁定路径投递", seen.get("flush") is False,
          str(seen.get("flush")))
    check("H-20 响应说明已入队待投递",
          gm.get("status") == "pending" and gm.get("enqueued", True) is not False,
          json.dumps(gm, ensure_ascii=False)[:90])


def test_terminal_error_leaves_trace():
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"
    hub, task = make_hub()
    task.state = "RUNNING"
    original = memory_pipeline.record_terminal

    def boom(**kwargs):
        raise RuntimeError("pipeline_exploded")

    memory_pipeline.record_terminal = boom
    try:
        async def scenario():
            await hub._terminal_fail(task, "TIMED_OUT", "retryable", "task_timeout", "hub")
            await asyncio.sleep(0.3)

        asyncio.run(scenario())
    finally:
        memory_pipeline.record_terminal = original
        os.environ["UAI_GRAPH_OUTCOMES"] = "0"
    records = [row.get("record") for row in hub.ledger.records()]
    check("H-21 终态记忆失败要留痕", "MEMORY_OUTCOME" in records, str(records))



def test_duplicate_delivery_is_deduped():
    payload_intent = base(intent_id="int-h6")
    calls = {"post": 0}

    def post(url, payload, timeout):
        calls["post"] += 1
        return {"ok": True}

    out = MW.write_intent(payload_intent, post=post, exists=lambda p: True,
                          verify=lambda p, path=None: "verified")
    check("H-22 已存在则不重复投递", calls["post"] == 0, str(calls["post"]))
    check("H-23 重复投递标记 already_present",
          out["reason"] == "already_present", str(out["reason"]))
    check("H-24 重复投递仍算已写入", out["written"] is True, str(out["written"]))

    out2 = MW.write_intent(payload_intent, post=post, exists=lambda p: False,
                           verify=lambda p, path=None: "verified")
    check("H-25 不存在时正常投递一次", calls["post"] == 1, str(calls["post"]))
    check("H-26 正常投递无 already_present", out2.get("reason") is None, str(out2.get("reason")))



def test_deep_nested_credential():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": "password: hunter2"}}}}}}}
    r = MI.validate_intent(base(refs=deep))
    check("H-27 深层 refs 凭据也被拒（无深度截断）",
          r == "invalid_intent:credential_like_text", str(r))


def test_credential_row_is_purged():
    db = Path(tempfile.mkdtemp()) / "purge.db"
    ob = MO.MemoryOutbox(db)
    ob.enqueue(base(intent_id="int-h7"))
    original = MW.write_intent
    MW.write_intent = lambda intent, **kw: {"written": False, "path": None,
                                            "degraded": False,
                                            "verified": "unverified",
                                            "reason": "credential_like_text"}
    try:
        MW.drain(ob)
    finally:
        MW.write_intent = original
    row = ob.get("int-h7")
    check("H-28 凭据行被丢弃", row["status"] == "dead_letter", row["status"])
    check("H-29 凭据内容不再留在库里", row["payload"] == "{}", row["payload"][:40])


def test_delivery_ack_reports_lost_update():
    db = Path(tempfile.mkdtemp()) / "ack.db"
    ob = MO.MemoryOutbox(db)
    ob.enqueue(base(intent_id="int-h8"))
    ob.claim_due()
    ob.recover_stale(older_than_s=0, now=time.time() + 1)
    applied = ob.mark_delivered("int-h8", verified="verified", write_path="graphiti")
    check("H-30 丢失更新要能被调用方发现", applied is False, str(applied))
    check("H-31 行仍是 retry（会再投）", ob.get("int-h8")["status"] == "retry",
          ob.get("int-h8")["status"])


def test_concurrent_enqueue():
    db = Path(tempfile.mkdtemp()) / "conc.db"
    ob = MO.MemoryOutbox(db)
    failures = []
    lock = threading.Lock()

    def worker(idx):
        for j in range(8):
            res = ob.enqueue(base(intent_id="c-%d-%d" % (idx, j)))
            if not res["enqueued"]:
                with lock:
                    failures.append(res)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("H-32 并发入队不丢（无 database is locked）", failures == [], str(failures[:2]))
    check("H-33 并发入队条数正确", ob.stats()["total"] == 32, str(ob.stats()["total"]))


def test_deferred_is_not_a_failure():
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"
    hub, _ = make_hub()
    original = memory_pipeline.record_verdict
    memory_pipeline.record_verdict = lambda **kw: {
        "enqueued": True, "written": False, "status": "pending",
        "intent_id": "int-h9", "reason": "deferred"}
    try:
        msg = Msg({"task_id": "t-hub-h", "verdict": "通过", "checker": "tester"})
        asyncio.run(hub.on_verdict(msg))
    finally:
        memory_pipeline.record_verdict = original
        os.environ["UAI_GRAPH_OUTCOMES"] = "0"
    records = [row.get("record") for row in hub.ledger.records()]
    check("H-34 入队成功不算记忆写丢", "MEMORY_OUTCOME" not in records, str(records))


def test_redelivery_deduped_end_to_end():
    db = Path(tempfile.mkdtemp()) / "dedup.db"
    ob = MO.MemoryOutbox(db)
    ob.enqueue(base(intent_id="int-h10"))
    store = set()
    posts = []

    def post(url, payload, timeout):
        posts.append(payload["name"])
        store.add(payload["name"])
        return {"ok": True}

    def exists(payload):
        return payload["name"] in store

    kwargs = {"post": post, "exists": exists, "verify": lambda p, path=None: "verified"}
    MW.drain(ob, **kwargs)
    ob.replay("int-h10")          # 模拟崩溃后重放
    MW.drain(ob, **kwargs)
    check("H-35 重放不会二次投递 Graphiti", len(posts) == 1, str(posts))
    check("H-36 重放后仍是 delivered", ob.get("int-h10")["status"] == "delivered",
          ob.get("int-h10")["status"])



def test_replay_refuses_inflight():
    db = Path(tempfile.mkdtemp()) / "rp.db"
    ob = MO.MemoryOutbox(db)
    ob.enqueue(base(intent_id="int-h11"))
    ob.claim_due()
    ok = ob.replay("int-h11")
    check("H-37 投递中的行不允许 replay", ok is False, str(ok))
    check("H-38 状态仍是 inflight", ob.get("int-h11")["status"] == "inflight",
          ob.get("int-h11")["status"])


def test_duplicate_verdict_is_not_a_failure():
    os.environ["UAI_GRAPH_OUTCOMES"] = "1"
    hub, _ = make_hub()
    original = memory_pipeline.record_verdict
    memory_pipeline.record_verdict = lambda **kw: {
        "enqueued": False, "duplicate": True, "written": False,
        "reason": "duplicate_intent_id", "intent_id": "verdict:t-hub-h:通过"}
    try:
        msg = Msg({"task_id": "t-hub-h", "verdict": "通过", "checker": "tester"})
        asyncio.run(hub.on_verdict(msg))
    finally:
        memory_pipeline.record_verdict = original
        os.environ["UAI_GRAPH_OUTCOMES"] = "0"
    records = [row.get("record") for row in hub.ledger.records()]
    check("H-39 重复裁定不写 MEMORY_OUTCOME", "MEMORY_OUTCOME" not in records, str(records))


def test_purge_removes_plaintext():
    db = Path(tempfile.mkdtemp()) / "p.db"
    ob = MO.MemoryOutbox(db)
    ob.enqueue(base(intent_id="int-h12", claim="正常内容", refs={}))
    ob.purge("int-h12", "credential_like_text")
    raw = b""
    for suffix in ("", "-wal", "-shm"):
        path = Path(str(db) + suffix)
        if path.exists():
            raw += path.read_bytes()
    check("H-40 purge 后行内容清空", ob.get("int-h12")["payload"] == "{}",
          ob.get("int-h12")["payload"][:20])
    needle = "正常内容".encode("utf-8")
    check("H-41 purge 后库文件不含原文", needle not in raw, "bytes=%d" % len(raw))


def test_enqueue_is_bounded():
    import sqlite3
    db = Path(tempfile.mkdtemp()) / "lock.db"
    ob = MO.MemoryOutbox(db)
    holder = sqlite3.connect(str(db), isolation_level=None, timeout=0.1)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO intents (intent_id,kind,payload,status,attempts,"
                   "next_attempt_at,created_at,updated_at,degraded) "
                   "VALUES ('hold','decision','{}','pending',0,0,0,0,0)")
    started = time.time()
    res = ob.enqueue(base(intent_id="int-h13"))
    elapsed = time.time() - started
    holder.execute("ROLLBACK")
    holder.close()
    check("H-42 写锁下入队有界（<4s）", elapsed < 4.0, "%.2fs" % elapsed)
    check("H-43 写锁下明确报失败而不是假装成功", res["enqueued"] is False, str(res))

def main() -> int:
    test_credential_fields()
    test_credential_phrasing()
    test_payload_never_holds_secret()
    test_concurrent_claim()
    test_mark_failed_after_delivered()
    test_verify_honesty()
    test_audit_unverified_gap()
    test_verdict_does_not_block()
    test_terminal_error_leaves_trace()
    test_duplicate_delivery_is_deduped()
    test_deep_nested_credential()
    test_credential_row_is_purged()
    test_delivery_ack_reports_lost_update()
    test_concurrent_enqueue()
    test_deferred_is_not_a_failure()
    test_redelivery_deduped_end_to_end()
    test_replay_refuses_inflight()
    test_duplicate_verdict_is_not_a_failure()
    test_purge_removes_plaintext()
    test_enqueue_is_bounded()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
