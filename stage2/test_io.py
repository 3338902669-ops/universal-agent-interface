"""Stage 2 入站/出站自检：inbound.py + event_bridge.py。

跑法：python test_io.py ；退出码 0 = 全通过。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

import event_bridge
import inbound

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def expect_error(fn, name, needle=None):
    try:
        fn()
    except inbound.InboundError as e:
        ok = (needle is None) or (needle in str(e))
        check(name, ok, str(e)[:70])
        return
    except Exception as e:
        check(name, False, "wrong error %s: %s" % (type(e).__name__, e))
        return
    check(name, False, "no error raised")


BASE_MSG = {
    "subject": "user-1",
    "message": {
        "messageId": "msg-001",
        "parts": [{"text": "请执行一次只读检查"}],
    },
}


# ---------------- 入站 ----------------
def test_inbound():
    env = inbound.envelope_from_a2a(BASE_MSG, created_by="user-1", target_interface="dsh")

    check("1 基本信封：11 个必填字段齐全",
          all(k in env for k in ("contract_version", "envelope_id", "task_id", "idempotency_key",
                                 "target", "instruction", "timeout_s", "cls", "created_by",
                                 "created_at", "trace_id")),
          "%d keys" % len(env))
    check("2 contract_version=1.0", env["contract_version"] == "1.0", env["contract_version"])
    check("3 target 是单键对象", list(env["target"]) == ["interface_id"], str(env["target"]))
    check("4 instruction 原样保留", env["instruction"] == "请执行一次只读检查", env["instruction"][:20])
    check("5 信封字节数 < 262144",
          len(json.dumps(env, ensure_ascii=False).encode()) < inbound.MAX_ENVELOPE_BYTES,
          "%d bytes" % len(json.dumps(env, ensure_ascii=False).encode()))

    # 幂等键：同 messageId + 同正文 → 同键；改正文 → 不同键
    env2 = inbound.envelope_from_a2a(BASE_MSG, created_by="user-1", target_interface="dsh")
    check("6 同 messageId 同正文 → 同一 idempotency_key",
          env["idempotency_key"] == env2["idempotency_key"], env["idempotency_key"][:16])
    changed = json.loads(json.dumps(BASE_MSG))
    changed["message"]["parts"][0]["text"] = "换一个任务"
    env3 = inbound.envelope_from_a2a(changed, created_by="user-1", target_interface="dsh")
    check("7 同 messageId 改正文保留键，由中枢拒绝语义冲突",
          env["idempotency_key"] == env3["idempotency_key"], env3["idempotency_key"][:16])
    check("8 task_id 每次都新（新提交新信封）", env["task_id"] != env2["task_id"], "")

    # target 二选一
    expect_error(lambda: inbound.envelope_from_a2a(BASE_MSG, created_by="u"), "9 无 target 被拒", "exactly_one")
    expect_error(lambda: inbound.envelope_from_a2a(BASE_MSG, created_by="u",
                                                  target_interface="dsh", target_capability="runtime_exec"),
                 "10 双 target 被拒", "exactly_one")

    # 审批
    expect_error(lambda: inbound.envelope_from_a2a(BASE_MSG, created_by="u",
                                                  target_interface="dsh", cls="external"),
                 "11 cls=external 无审批被拒", "approval")
    expect_error(lambda: inbound.envelope_from_a2a(BASE_MSG, created_by="u", target_interface="dsh",
                                                  cls="external", approval={"credential_id": "c1"}),
                 "12 未接可信审批时即使有凭据也拒绝", "approval")

    # 附件指纹
    bad_part = {"message": {"messageId": "m2", "parts": [
        {"text": "带附件"}, {"url": "https://x/y.png", "media_type": "image/png"}]}}
    expect_error(lambda: inbound.envelope_from_a2a(bad_part, created_by="u", target_interface="dsh"),
                 "13 附件无 sha256 被拒", "sha256")

    good_part = {"message": {"messageId": "m3", "parts": [
        {"text": "带附件"},
        {"url": "https://x/y.png", "media_type": "image/png",
         "metadata": {"uai/sha256": "a" * 64, "uai/bytes": 12}}]}}
    env4 = inbound.envelope_from_a2a(good_part, created_by="u", target_interface="dsh")
    check("14 附件四字段齐全可过",
          env4["attachments"][0]["sha256"] == "a" * 64 and env4["attachments"][0]["bytes"] == 12,
          str(env4["attachments"][0])[:60])

    # 超时与分类
    expect_error(lambda: inbound.envelope_from_a2a(BASE_MSG, created_by="u",
                                                  target_interface="dsh", timeout_s=0),
                 "15 timeout_s=0 被拒", "timeout")
    expect_error(lambda: inbound.envelope_from_a2a(BASE_MSG, created_by="u",
                                                  target_interface="dsh", cls="bogus"),
                 "16 非法 cls 被拒", "cls")

    # 凭据夹带
    secret_msg = {"message": {"messageId": "m4", "parts": [{"text": "api_key=abc123"}]}}
    expect_error(lambda: inbound.envelope_from_a2a(secret_msg, created_by="u", target_interface="dsh"),
                 "17 正文夹带凭据字样被拒", "credential")

    # 缺 messageId
    expect_error(lambda: inbound.envelope_from_a2a({"message": {"parts": [{"text": "x"}]}},
                                                  created_by="u", target_interface="dsh"),
                 "18 缺 messageId 被拒", "message_id")


# ---------------- 出站 ----------------
def test_bridge():
    tmp = Path(tempfile.mkdtemp(prefix="uai-bridge-"))
    (tmp / "data").mkdir(parents=True)
    ledger = tmp / "data" / "ledger.jsonl"
    outbox = tmp / "data" / "a2a-outbox.jsonl"

    records = [
        {"record": "TASK_EVENT", "ts": "2026-11-12T00:00:01Z", "task_id": "t1", "seq": 1,
         "kind": "accepted"},
        {"record": "TASK_EVENT", "ts": "2026-11-12T00:00:02Z", "task_id": "t1", "seq": 2,
         "kind": "claimed"},
        {"record": "TASK_EVENT", "ts": "2026-11-12T00:00:03Z", "task_id": "t1", "seq": 3,
         "kind": "heartbeat"},
        {"record": "TASK_EVENT", "ts": "2026-11-12T00:00:04Z", "task_id": "t1", "seq": 4,
         "kind": "result_ready",
         "result_ref": {"uri": "file:///r.json", "sha256": "b" * 64, "bytes": 9,
                        "media_type": "application/json"}},
        {"record": "TASK_EVENT", "ts": "2026-11-12T00:00:05Z", "task_id": "t1", "seq": 5,
         "kind": "completed", "verdict": "通过"},
    ]
    ledger.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n",
                      encoding="utf-8")

    bridge = event_bridge.EventBridge(tmp, outbox)
    size_before = ledger.stat().st_size
    events = bridge.drain()
    bridge.write(events)
    size_after = ledger.stat().st_size

    kinds = [e.get("kind") for e in events]
    check("19 出站：5 条内部事件 → 4 条 A2A 事件（heartbeat 不投影）",
          len(events) == 4 and "heartbeat" not in json.dumps(events), str(kinds))
    check("20 accepted → task 对象",
          events[0]["kind"] == "task" and events[0]["task"]["id"] == "t1", events[0]["kind"])
    check("21 claimed → status_update WORKING",
          events[1]["kind"] == "status_update"
          and events[1]["status"]["state"] == "TASK_STATE_WORKING", events[1]["status"]["state"])
    check("22 result_ready → artifact_update，指纹进 metadata",
          events[2]["kind"] == "artifact_update"
          and events[2]["artifact"]["metadata"]["uai/sha256"] == "b" * 64, "")
    check("23 completed 带裁定 → message=通过 且 final=True",
          events[3]["kind"] == "status_update" and events[3]["final"] is True
          and events[3]["status"]["message"]["parts"][0]["text"] == "通过", "")

    # 幂等：再 drain 一次不应重复
    again = bridge.drain()
    check("24 重复跟随不产生重复事件", again == [], "%d" % len(again))

    # 增量：追加新事件后只吐新的
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "TASK_EVENT", "ts": "2026-11-12T00:00:06Z",
                            "task_id": "t1", "seq": 6, "kind": "cancelled"},
                           ensure_ascii=False) + "\n")
    more = bridge.drain()
    bridge.write(more)
    check("25 增量跟随：只吐新增一条", len(more) == 1 and more[0]["final"] is True,
          "%d" % len(more))
    bridge.close()

    # 桥不写账本：跟随前后账本尺寸必须一致
    check("26 桥只读账本：跟随前后尺寸不变", size_before == size_after,
          "%d → %d" % (size_before, size_after))

    lines = outbox.read_text(encoding="utf-8").strip().split("\n")
    check("27 出站信箱是只追加 JSONL（4 首轮 + 1 增量 = 5）", len(lines) == 5, "%d lines" % len(lines))

    shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    test_inbound()
    test_bridge()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
