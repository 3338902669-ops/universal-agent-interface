"""A-04:官方 A2A HTTP message/send 入站闭环。

链路:HTTP POST(JSON-RPC message/send)→ a2a_server → inbound_bridge → hub → 终态查询。
前置:真 NATS + hub 已在 127.0.0.1:43321;NATS_URL 可覆盖。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:43321")

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    os.environ["NATS_URL"] = NATS_URL
    import a2a_server
    from pathlib import Path

    base = Path(__file__).resolve().parent.parent / "prototype"
    srv, _thread = a2a_server.serve_in_thread(base, None, 0)
    port = srv.server_address[1]

    # 1. message/send 正常受理
    req = {"jsonrpc": "2.0", "id": 1, "method": "message/send",
           "params": {"message": {"messageId": "a04-msg-" + __import__("uuid").uuid4().hex[:8],
                                   "parts": [{"text": "HTTP 入站探测,只回 OK"}]},
                       "target_interface": "dsh", "timeout_s": 300, "cls": "readonly",
                       "created_by": "a04-http"}}
    data = json.dumps(req).encode()
    r = urllib.request.Request("http://127.0.0.1:%d/" % port, data=data,
                               headers={"Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(r, timeout=30).read().decode())
    task_id = (resp.get("result") or {}).get("task", {}).get("id")
    check("A-04 message/send 受理", bool(task_id), str(resp)[:120])
    check("A-04 初始 state=submitted",
          (resp.get("result") or {}).get("task", {}).get("status", {}).get("state") == "submitted", "")

    # 2. GET /tasks/<id> 可投影查询
    r2 = urllib.request.urlopen("http://127.0.0.1:%d/tasks/%s" % (port, task_id), timeout=15)
    task = json.loads(r2.read().decode())
    check("A-04 GET task 可查询", task.get("id") == task_id, str(task)[:100])

    # 3. 不支持的方法明确拒绝
    req_bad = {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {}}
    r3 = urllib.request.Request("http://127.0.0.1:%d/" % port, data=json.dumps(req_bad).encode(),
                                headers={"Content-Type": "application/json"})
    resp3 = json.loads(urllib.request.urlopen(r3, timeout=15).read().decode())
    check("A-04 非法 method 明确拒绝(-32601)",
          resp3.get("error", {}).get("code") == -32601, str(resp3.get("error"))[:80])

    # 4. 坏 JSON 解析错误
    try:
        r4 = urllib.request.Request("http://127.0.0.1:%d/" % port, data=b"{bad",
                                    headers={"Content-Type": "application/json"})
        resp4 = json.loads(urllib.request.urlopen(r4, timeout=15).read().decode())
        check("A-04 坏 JSON 返回 parse_error(-32700)",
              resp4.get("error", {}).get("code") == -32700, "")
    except Exception as e:
        check("A-04 坏 JSON 返回 parse_error(-32700)", False, str(e)[:80])

    srv.shutdown()
    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
