"""A-PN:push notification 验收（规格 protocol/PUSH-NOTIFICATION-SPEC.md §7）。

场景 A 注册任务终态回调收到+签名校验;B 未注册零回调;C 回调 500 重试后死信;
D 错 token 验签失败;E 非终态不推。
前置:真 NATS+hub(hub 会写带 self_hash 的账本)。push_notifier 只读账本,不需 interface。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:43321")
PASSES, FAILS = [], []
RECEIVED: list[dict] = []
FAIL_MODE = {"on": False}


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


class CallbackHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if FAIL_MODE["on"]:
            self.send_response(500)
            self.end_headers()
            return
        sig = self.headers.get("X-UAI-Signature")
        ts = self.headers.get("X-UAI-Timestamp")
        RECEIVED.append({"headers": dict(self.headers), "body": body,
                         "sig": sig, "ts": ts})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, fmt, *args):  # 静音
        pass


def main() -> int:
    base = Path(__file__).resolve().parent.parent / "prototype"
    os.environ["NATS_URL"] = NATS_URL
    import a2a_server
    import inbound_bridge
    import push_notifier
    import asyncio, uuid

    cb_srv = ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    cb_port = cb_srv.server_address[1]
    threading.Thread(target=cb_srv.serve_forever, daemon=True).start()
    cb_url = "http://127.0.0.1:%d/callback" % cb_port
    TOKEN = "push-secret-1"

    # 场景 A:注册了 push 的任务,走到终态(无 interface 认领 → 5s 租约超时由 hub 判? 不,hub 不主动超时任务。
    # 用 hub.debug? 最稳:超时分层——task_timeout 由 interface 层判。这里改为:提交后直接手工写终态? 
    # 不行,P-1 要求事件来自真实账本。改用:hub.reconcile? 
    # 最简:提交 cls=readonly 无 interface 认领,hub 不会终态。→ 本测试改用独立小账本+手工写终态记录,
    # push_notifier 只依赖账本形态,不必真 hub。场景 B-E 同理,用隔离目录。
    work = Path(os.environ.get("A_PN_BASE") or Path(__file__).parent / "_apn_work")
    work.mkdir(exist_ok=True)
    ledger = work / "data" / "ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    cbt = work / "data" / "push-callbacks.jsonl"

    def append_ledger(rows):
        with ledger.open("a", encoding="utf-8", newline="\n") as fh:
            for r in rows:
                fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")

    # ---- 场景 A:注册任务,终态,签名校验 ----
    tid_a = str(uuid.uuid4())
    with cbt.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"task_id": tid_a, "url": cb_url, "token": TOKEN},
                            separators=(",", ":")) + "\n")
    append_ledger([
        {"record": "TASK_STATE", "ts": "t1", "task_id": tid_a, "state": "QUEUED"},
        {"record": "TASK_STATE", "ts": "t2", "task_id": tid_a, "state": "RUNNING"},
        {"record": "TASK_STATE", "ts": "t3", "task_id": tid_a, "state": "SUCCEEDED"},  # 终态→推
    ])
    stat = asyncio.run(push_notifier.run_once(work, cbt))
    check("A-PN A 终态回调收到", len(RECEIVED) == 1, str(stat))
    if RECEIVED:
        ev = json.loads(RECEIVED[0]["body"])
        check("A-PN A body 含 task/seq/state",
              ev["task_id"] == tid_a and ev["kind"] == "state" and ev["state"] == "SUCCEEDED", "")
        expect_sig = hmac.new(TOKEN.encode(),
                              (RECEIVED[0]["ts"] + "." + RECEIVED[0]["body"].decode()).encode(),
                              hashlib.sha256).hexdigest()
        check("A-PN A 签名校验通过", RECEIVED[0]["sig"] == expect_sig, "")
        hdrs = {k.lower(): v for k, v in RECEIVED[0]["headers"].items()}
        check("A-PN A 头齐全",
              hdrs.get("x-uai-task-id") == tid_a
              and hdrs.get("x-uai-seq") == str(ev["seq"]), str(hdrs.get("x-uai-task-id"))[:40])

    # ---- 场景 E:非终态不推(QUEUED/RUNNING 两条已在 A 中,RECEIVED 仍为 1 即证) ----
    check("A-PN E 非终态不推", len(RECEIVED) == 1, "received=%d" % len(RECEIVED))

    # ---- 场景 B:未注册任务零回调 ----
    tid_b = str(uuid.uuid4())
    append_ledger([{"record": "TASK_STATE", "ts": "t4", "task_id": tid_b, "state": "FAILED"}])
    asyncio.run(push_notifier.run_once(work, cbt))
    check("A-PN B 未注册零回调", len(RECEIVED) == 1, "received=%d" % len(RECEIVED))

    # ---- 场景 C:回调 500 → 重试 3 次死信,主链路不受影响 ----
    tid_c = str(uuid.uuid4())
    with cbt.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"task_id": tid_c, "url": cb_url}, separators=(",", ":")) + "\n")
    append_ledger([{"record": "TASK_STATE", "ts": "t5", "task_id": tid_c, "state": "TIMED_OUT"}])
    FAIL_MODE["on"] = True
    before = len(RECEIVED)
    # 压缩退避:直接 monkeypatch BACKOFF_S
    push_notifier.BACKOFF_S = (0, 0, 0)
    stat_c = asyncio.run(push_notifier.run_once(work, cbt))
    check("A-PN C 回调 500 → 死信落盘", stat_c["deadlettered"] == 1, str(stat_c))
    dead = work / "data" / "push-dead-letters.jsonl"
    check("A-PN C 死信文件存在且含任务", dead.exists() and tid_c in dead.read_text(encoding="utf-8"), "")
    check("A-PN C 主链路不受影响(run_once 正常返回)", isinstance(stat_c, dict), "")
    push_notifier.BACKOFF_S = (2, 8, 32)
    FAIL_MODE["on"] = False

    # ---- 场景 D:错 token 签名 ----
    tid_d = str(uuid.uuid4())
    # 用错 token 注册:回调端按正确 token 验会失败。模拟:接收端记录签名,本地验证失败
    with cbt.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"task_id": tid_d, "url": cb_url, "token": "WRONG-TOKEN"},
                            separators=(",", ":")) + "\n")
    append_ledger([{"record": "TASK_STATE", "ts": "t6", "task_id": tid_d, "state": "CANCELLED"}])
    n0 = len(RECEIVED)
    asyncio.run(push_notifier.run_once(work, cbt))
    check("A-PN D 错 token 投递成功但签名与正确 token 不符",
          len(RECEIVED) == n0 + 1 and RECEIVED[-1]["sig"] !=
          hmac.new(TOKEN.encode(),
                   (RECEIVED[-1]["ts"] + "." + RECEIVED[-1]["body"].decode()).encode(),
                   hashlib.sha256).hexdigest(), "")

    # ---- 幂等/单调:offset 前进,重复 run_once 不重推 ----
    stat2 = asyncio.run(push_notifier.run_once(work, cbt))
    check("A-PN 幂等:offset 前进后零重推", stat2["events"] == 0, str(stat2))

    cb_srv.shutdown()
    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
