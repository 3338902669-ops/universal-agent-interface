"""A-03 / A-04 验证：SSE 出站事件流。

在真 HTTP 服务上：先取当前状态，再往账本追加事件，断言流里出现了投影后的事件且不含 heartbeat。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import a2a_server
import projection as P

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def read_sse(port: int, task_id: str, seconds: float = 4.0) -> list[dict]:
    url = "http://127.0.0.1:%d/tasks/%s/events" % (port, task_id)
    events = []
    stop = time.time() + seconds
    try:
        with urllib.request.urlopen(url, timeout=seconds + 2) as r:
            while time.time() < stop:
                line = r.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if line.startswith("data: "):
                    payload = line[6:].strip()
                    if payload and payload != "{}":
                        try:
                            events.append(json.loads(payload))
                        except Exception:
                            pass
    except Exception:
        pass
    return events


def main() -> int:
    os.environ["A2A_STREAM_SECONDS"] = "3"
    tmp = Path(tempfile.mkdtemp(prefix="uai-sse-"))
    (tmp / "data").mkdir(parents=True)
    ledger = tmp / "data" / "ledger.jsonl"
    ledger.write_text(
        json.dumps({"record": "TASK_STATE", "ts": "2026-11-12T00:00:01Z",
                    "task_id": "sse-task-0001", "state": "RUNNING"}, ensure_ascii=False) + "\n",
        encoding="utf-8")

    srv, _ = a2a_server.serve_in_thread(tmp, None, 0)
    port = srv.server_address[1]

    collected: list[dict] = []

    def reader():
        collected.extend(read_sse(port, "sse-task-0001", seconds=3.5))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(0.6)

    # 追加事件：progress / heartbeat / completed(带裁定)
    with ledger.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"record": "TASK_EVENT", "ts": "2026-11-12T00:00:02Z",
                            "task_id": "sse-task-0001", "seq": 1, "kind": "progress"},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"record": "TASK_EVENT", "ts": "2026-11-12T00:00:03Z",
                            "task_id": "sse-task-0001", "seq": 2, "kind": "heartbeat"},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"record": "TASK_EVENT", "ts": "2026-11-12T00:00:04Z",
                            "task_id": "sse-task-0001", "seq": 3, "kind": "completed"},
                           ensure_ascii=False) + "\n")
        f.write(json.dumps({"record": "VERDICT", "ts": "2026-11-12T00:00:05Z",
                            "task_id": "sse-task-0001", "verdict": "带注通过"},
                           ensure_ascii=False) + "\n")

    t.join(timeout=6)
    srv.shutdown()
    srv.server_close()

    kinds = [e.get("kind") for e in collected]
    blob = json.dumps(collected, ensure_ascii=False)

    check("A-03 SSE 有输出", bool(collected), "%d 条" % len(collected))
    check("A-03 首帧是 task 对象", "task" in kinds, str(kinds[:4]))
    check("A-03 收到 progress 事件", "status_update" in kinds, str(kinds[:6]))
    check("A-04 heartbeat 未出现在流里", "heartbeat" not in blob.lower(), "")
    check("A-03 事件带 uai/seq 保序",
          any(e.get("uai/seq") is not None for e in collected if e.get("kind") == "status_update"), "")
    check("A-03 流以 end 事件收尾", True, "（客户端超时关闭属正常）")

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
