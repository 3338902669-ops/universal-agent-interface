"""A-07:A2A SSE 事件流出站(task 状态推流)。

前置:真 NATS+hub。提交一个任务,然后订阅 /tasks/<id>/events,应在时限内收到 task 事件。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:43321")
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    os.environ["NATS_URL"] = NATS_URL
    os.environ.setdefault("A2A_STREAM_SECONDS", "6")
    import a2a_server
    import inbound_bridge
    import asyncio
    import uuid

    base = Path(__file__).resolve().parent.parent / "prototype"
    srv, _t = a2a_server.serve_in_thread(base, None, 0)
    port = srv.server_address[1]

    async def submit():
        req = {"message": {"messageId": "a07-" + uuid.uuid4().hex[:8],
                            "parts": [{"text": "SSE 出站探测"}]}}
        return await inbound_bridge.submit(nats_url=NATS_URL, request=req,
                                           created_by="a07", target_interface="dsh",
                                           timeout_s=300, contract_version="1.0")
    out = asyncio.run(submit())
    task_id = out["task_id"]
    check("A-07 任务已受理", out["accepted"] is True, task_id[:8])

    # 订阅 SSE,读若干行
    try:
        r = urllib.request.urlopen("http://127.0.0.1:%d/tasks/%s/events" % (port, task_id), timeout=15)
        lines = []
        for _ in range(20):
            line = r.readline()
            if not line:
                break
            lines.append(line.decode("utf-8", "replace").strip())
            if len(lines) >= 6:
                break
        r.close()
        data_lines = [l for l in lines if l.startswith("data: ")]
        got_task = any("task" in l and task_id[:8] in l for l in data_lines)
        check("A-07 SSE 收到 task 事件", bool(data_lines) and got_task,
              "%d data lines" % len(data_lines))
        check("A-07 SSE Content-Type 正确", r.headers.get_content_type() == "text/event-stream",
              r.headers.get_content_type())
    except Exception as e:
        check("A-07 SSE 事件流", False, str(e)[:100])

    srv.shutdown()
    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
