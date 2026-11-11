"""a2a_server 端到端自检：起真 HTTP 服务、喂真账本、核投影结果。

跑法：python test_a2a_server.py ；退出码 0 = 全通过。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import urllib.request
from pathlib import Path

import a2a_server
import projection as P

FAILS = []
PASSES = []


def check(name, cond, detail=""):
    if cond:
        PASSES.append(name)
        print("PASS  %-56s %s" % (name, detail))
    else:
        FAILS.append(name)
        print("FAIL  %-56s %s" % (name, detail))


def get(port, path):
    url = "http://127.0.0.1:%d%s" % (port, path)
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8") or "{}")


FIXTURE = [
    {"record": "TASK_STATE", "ts": "2026-11-12T00:00:01Z", "task_id": "t-success-noverdict",
     "state": "SUCCEEDED", "trigger": "completed"},
    {"record": "TASK_STATE", "ts": "2026-11-12T00:00:02Z", "task_id": "t-success-pass",
     "state": "SUCCEEDED", "trigger": "completed"},
    {"record": "VERDICT", "ts": "2026-11-12T00:00:03Z", "task_id": "t-success-pass",
     "verdict": "通过", "checker": "tianshu"},
    {"record": "TASK_STATE", "ts": "2026-11-12T00:00:04Z", "task_id": "t-legacy",
     "state": "SUCCEEDED", "trigger": "completed"},
    {"record": "VERDICT", "ts": "2026-11-12T00:00:05Z", "task_id": "t-legacy",
     "verdict": "驳回", "checker": "old"},
    {"record": "TASK_STATE", "ts": "2026-11-12T00:00:06Z", "task_id": "t-timeout",
     "state": "TIMED_OUT", "trigger": "terminal"},
    {"record": "TASK_STATE", "ts": "2026-11-12T00:00:07Z", "task_id": "t-disp",
     "state": "DISPATCHED", "trigger": "dispatch"},
    {"record": "INTERFACE_HEARTBEAT", "ts": "2026-11-12T00:00:08Z", "interface_id": "dsh"},
    {"record": "TASK_EVENT", "ts": "2026-11-12T00:00:09Z", "task_id": "t-disp",
     "kind": "heartbeat", "seq": 2},
]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="uai-a2a-"))
    (tmp / "data").mkdir(parents=True)
    (tmp / "data" / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in FIXTURE) + "\n", encoding="utf-8"
    )

    card = P.project_agent_card(
        interface_id="dsh",
        contract_version="1.0",
        control_subject="iface.dsh.control",
        public_url="https://agents.example.com/dsh",
        capabilities=[{"name": "runtime_exec", "description": "执行", "duration_class": "short"}],
    )

    srv, _ = a2a_server.serve_in_thread(tmp, card, 0)
    port = srv.server_address[1]
    try:
        code, body = get(port, "/.well-known/agent-card.json")
        check("1 agent-card 可取且含 supported_interfaces",
              code == 200 and body.get("supported_interfaces") and
              body["supported_interfaces"][0]["protocol_binding"] == "NATS",
              str(code))
        check("2 agent-card 不泄露内网 subject",
              "iface.dsh.control" not in json.dumps(body), "")

        code, body = get(port, "/tasks")
        check("3 /tasks 列出全部任务", code == 200 and len(body.get("tasks", [])) == 5,
              "%d tasks" % len(body.get("tasks", [])))

        code, body = get(port, "/tasks/t-success-noverdict")
        txt = json.dumps(body, ensure_ascii=False)
        check("4 SUCCEEDED 无裁定 → completed + 待核验",
              code == 200 and body["status"]["state"] == "TASK_STATE_COMPLETED"
              and "待核验" in txt, body["status"]["state"])

        code, body = get(port, "/tasks/t-success-pass")
        msg = body["status"]["message"]["parts"][0]["text"]
        check("5 SUCCEEDED 有裁定 → message 恰为「通过」",
              code == 200 and msg == "通过"
              and body["status"]["metadata"]["uai/state"] == "SUCCEEDED", repr(msg))

        code, body = get(port, "/tasks/t-legacy")
        txt = json.dumps(body, ensure_ascii=False)
        check("6 历史「驳回」读出归一为「不通过」",
              code == 200 and "不通过" in txt and "驳回" not in txt, "")

        code, body = get(port, "/tasks/t-timeout")
        check("7 TIMED_OUT → FAILED 且带原因码",
              code == 200 and body["status"]["state"] == "TASK_STATE_FAILED"
              and "timed_out" in json.dumps(body), "")

        code, body = get(port, "/tasks/t-disp")
        check("8 DISPATCHED → SUBMITTED，原态留在 uai/state",
              code == 200 and body["status"]["state"] == "TASK_STATE_SUBMITTED"
              and body["status"]["metadata"]["uai/state"] == "DISPATCHED", "")

        code, body = get(port, "/tasks")
        keys_ok = all(set(item) == {"id", "state"} for item in body["tasks"])
        states_ok = all(
            item["state"] in {
                "TASK_STATE_SUBMITTED", "TASK_STATE_WORKING", "TASK_STATE_COMPLETED",
                "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED",
                "TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED",
                "TASK_STATE_UNSPECIFIED",
            }
            for item in body["tasks"]
        )
        # 心跳不是任务，不得出现在任务列表里
        check("9 /tasks 条目只含 {id,state} 且 state 均属 A2A 九值",
              code == 200 and keys_ok and states_ok,
              "%d items" % len(body["tasks"]))

        code, body = get(port, "/tasks/no-such-task")
        check("10 未知任务 → 404", code == 404, str(code))

        # 幂等与只读
        a = json.dumps(get(port, "/tasks/t-success-pass")[1], sort_keys=True, ensure_ascii=False)
        b = json.dumps(get(port, "/tasks/t-success-pass")[1], sort_keys=True, ensure_ascii=False)
        check("11 投影幂等：两次请求字节相同", a == b, "")

        before = (tmp / "data" / "ledger.jsonl").stat().st_size
        get(port, "/tasks")
        after = (tmp / "data" / "ledger.jsonl").stat().st_size
        check("12 服务只读：账本尺寸不变", before == after, "%d bytes" % after)
    finally:
        srv.shutdown()
        srv.server_close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
