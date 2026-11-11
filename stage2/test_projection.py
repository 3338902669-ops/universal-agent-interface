"""Stage 2 投影层自检。对应映射表 v1.4 §9.2 的 8 条验收判据。

直接跑：python test_projection.py；退出码 0 = 全通过。
"""

from __future__ import annotations

import copy
import io
import json
import re
import sys

import projection as P

CASES = []


def case(name):
    def deco(fn):
        CASES.append((name, fn))
        return fn

    return deco


# ---------- 判据 1：名片投影 ----------
@case("1 名片投影：capabilities 数组不撞 A2A capabilities 对象")
def t1():
    card = P.project_agent_card(
        interface_id="dsh",
        contract_version="1.0",
        control_subject="iface.dsh.control",
        public_url="https://agents.example.com/dsh",
        capabilities=[
            {"name": "runtime_exec", "description": "执行任务", "input_type": "text/plain",
             "output_type": "application/json", "duration_class": "short"},
        ],
    )
    assert isinstance(card["capabilities"], dict), "capabilities 必须是对象"
    assert isinstance(card["skills"], list) and card["skills"], "skills 必须是非空数组"
    assert card["skills"][0]["id"] == "runtime_exec"
    assert card["supported_interfaces"][0]["protocol_binding"] == "NATS"
    assert "iface.dsh.control" not in json.dumps(card), "不得泄露内网 subject"
    return card


# ---------- 判据 2：状态投影 ----------
@case("2 状态投影：八态 → 九值，DISPATCHED/CLAIMED 合并后靠 uai/state 保留精度")
def t2():
    mapping = {
        "QUEUED": "TASK_STATE_SUBMITTED",
        "DISPATCHED": "TASK_STATE_SUBMITTED",
        "CLAIMED": "TASK_STATE_WORKING",
        "RUNNING": "TASK_STATE_WORKING",
        "SUCCEEDED": "TASK_STATE_COMPLETED",
        "FAILED": "TASK_STATE_FAILED",
        "TIMED_OUT": "TASK_STATE_FAILED",
        "CANCELLED": "TASK_STATE_CANCELED",
    }
    out = {}
    for state, expect in mapping.items():
        st = P.project_status(state)
        assert st["state"] == expect, "%s → %s != %s" % (state, st["state"], expect)
        assert st["metadata"]["uai/state"] == state, "精度补偿丢失"
        out[state] = st["state"]
    return out


# ---------- 判据 3：SUCCEEDED 无裁定必须写「待核验」 ----------
@case("3 SUCCEEDED 无裁定 → message 必须是「待核验」")
def t3():
    st = P.project_status("SUCCEEDED")
    assert st["state"] == "TASK_STATE_COMPLETED"
    text = st["message"]["parts"][0]["text"]
    assert text == "待核验", "got %r" % text

    st2 = P.project_status("SUCCEEDED", verdict="通过")
    assert st2["message"]["parts"][0]["text"] == "通过"
    return {"no_verdict": text, "with_verdict": st2["message"]["parts"][0]["text"]}


# ---------- 判据 4：heartbeat 不出现在任何对外响应 ----------
@case("4 heartbeat 不投影")
def t4():
    ev = {"kind": "heartbeat", "task_id": "t1", "seq": 3}
    assert P.project_event(ev) is None
    base = {"task_id": "t1", "occurred_at": "2026-11-12T00:00:00Z"}
    for kind in ("accepted", "claimed", "progress", "completed", "failed", "cancelled"):
        out = P.project_event(dict(base, kind=kind))
        assert out is not None, "%s 应可投影" % kind
        assert "heartbeat" not in json.dumps(out)
    # result_ready 必须带结果指针（真账本的指针挂在 TASK_STATE 上，见 bridge）
    out = P.project_event(dict(base, kind="result_ready",
                               result_ref={"uri": "file:///r", "sha256": "c" * 64,
                                           "bytes": 3, "media_type": "text/plain"}))
    assert out is not None and out["kind"] == "artifact_update"
    return "ok"


# ---------- 判据 5：「驳回」出现在产出路径必须被拒 ----------
@case("5 产出路径拒绝「驳回」；读路径归一为「不通过」")
def t5():
    try:
        P.assert_writable_verdict("驳回")
    except P.ProjectionError as e:
        pass
    else:
        raise AssertionError("驳回 不应被写路径接受")

    assert P.canonicalize_verdict("驳回") == "不通过"
    assert P.canonicalize_verdict("通过") == "通过"

    try:
        P.project_status("SUCCEEDED", verdict="驳回")
    except P.ProjectionError:
        pass
    else:
        raise AssertionError("project_status 不应接受 驳回")

    for bad in ("完成", "PASS", ""):
        try:
            P.assert_writable_verdict(bad)
        except P.ProjectionError:
            continue
        raise AssertionError("非法裁定 %r 应被拒" % bad)
    return "ok"


# ---------- 判据 6：入站派生幂等键（调用生产实现，不自己重写公式） ----------
@case("6 入站派生 idempotency_key 可复算（调用 inbound 生产函数）")
def t6():
    import inbound

    a = inbound.derive_idempotency_key("user-1", "msg-1", "do it", {"k": 1})
    b = inbound.derive_idempotency_key("user-1", "msg-1", "do it", {"k": 1})
    c = inbound.derive_idempotency_key("user-1", "msg-1", "do it", {"k": 2})
    assert a == b, "同输入必须同值"
    assert a != c, "不同语义必须不同值"
    assert len(a) == 64, "sha256 hex 应为 64 位"

    env = inbound.envelope_from_a2a(
        {"subject": "user-1", "message": {"messageId": "msg-1", "parts": [{"text": "do it"}]}},
        created_by="user-1", target_interface="dsh",
    )
    expect = inbound.derive_idempotency_key("user-1", "msg-1", "do it", None)
    assert env["idempotency_key"] == expect, "信封里的键必须等于生产函数对同一输入的派生值"
    return a[:16]


# ---------- 判据 7：投影幂等 ----------
@case("7 投影幂等：同输入两次投影字节相同")
def t7():
    args = dict(task_id="t-9", internal_state="RUNNING", context_id="c-1",
                instruction="hello", timestamp="2026-11-12T00:00:00Z")
    a = json.dumps(P.project_task(**args), ensure_ascii=False, sort_keys=True)
    b = json.dumps(P.project_task(**args), ensure_ascii=False, sort_keys=True)
    assert a == b
    ev = {"kind": "progress", "task_id": "t-9", "occurred_at": "2026-11-12T00:00:00Z"}
    x = json.dumps(P.project_event(copy.deepcopy(ev)), ensure_ascii=False, sort_keys=True)
    y = json.dumps(P.project_event(copy.deepcopy(ev)), ensure_ascii=False, sort_keys=True)
    assert x == y
    return a[:40]


# ---------- 判据 8：投影失败不改变入参（不得原地改写调用方对象） ----------
@case("8 投影失败不修改入参、不抛非 ProjectionError 异常")
def t8():
    bad_state = {"task_id": "t", "internal_state": "NOPE"}
    snapshot = copy.deepcopy(bad_state)
    try:
        P.project_task(**bad_state)
    except P.ProjectionError:
        pass
    else:
        raise AssertionError("应抛 ProjectionError")
    assert bad_state == snapshot, "投影失败时不得修改入参"

    ev = {"kind": "result_ready", "task_id": "t", "result_ref": None}
    ev_snapshot = copy.deepcopy(ev)
    try:
        P.project_event(ev)
    except P.ProjectionError:
        pass
    else:
        raise AssertionError("应抛 ProjectionError")
    assert ev == ev_snapshot, "投影失败时不得修改事件对象"
    return "inputs untouched"


# ---------- 判据 9：对照官方 proto 的 TaskState 枚举（独立来源） ----------
@case("9 STATE_TO_A2A 的像集全部存在于官方 a2a.proto 的 TaskState 枚举")
def t9():
    import os
    proto = os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "a2a-official-spec", "a2a.proto",
    )
    if not os.path.exists(proto):
        raise AssertionError("缺少官方 proto，无法做独立比对：%s" % proto)
    text = io.open(proto, encoding="utf-8").read()
    m = re.search(r"enum\s+TaskState\s*\{(.*?)\}", text, re.S)
    assert m, "proto 里找不到 enum TaskState"
    official = set(re.findall(r"(TASK_STATE_[A-Z_]+)\s*=", m.group(1)))
    assert official, "proto 枚举解析为空"
    used = set(P.STATE_TO_A2A.values())
    assert used <= official, "出现 proto 未定义的值：%s" % (used - official)
    assert "TASK_STATE_REJECTED" not in used, "本契约任何内部态都不应投影为 REJECTED"
    return "%d/%d 值命中官方枚举" % (len(used), len(official))


# ---------- 回归：真账本出现过 result_ref 为 null 的 result_ready ----------
@case("10 回归：result_ref 为 null 的 result_ready 抛错而不是崩栈")
def t10():
    try:
        P.project_event({"kind": "result_ready", "task_id": "t1", "result_ref": None})
    except P.ProjectionError as e:
        assert "result_ref" in str(e), str(e)
        return str(e)
    raise AssertionError("应抛 ProjectionError，而不是 AttributeError")


def main() -> int:
    passed = failed = 0
    for name, fn in CASES:
        try:
            out = fn()
            passed += 1
            print("PASS  %-52s %s" % (name, str(out)[:80]))
        except Exception as e:
            failed += 1
            print("FAIL  %-52s %s: %s" % (name, type(e).__name__, e))
    print("\nRESULT: %d/%d passed" % (passed, len(CASES)))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
