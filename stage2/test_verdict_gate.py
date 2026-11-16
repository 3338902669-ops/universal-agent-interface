"""E-VG: 裁定门禁回归测试。

两条规则必须守住：
1. 未到终态的任务不得判「通过 / 带注通过」——没有产出就宣布成功是伪证
2. 未到终态允许判「不通过 / 作废 / 未核」——那是记录问题

用假 NATS 消息直接调 Hub.on_verdict，不依赖真实 hub 进程。
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

# 本套件用的是假任务 id：绝不往真图谱写记忆（UAI-08 的写入点默认开启）
os.environ["UAI_GRAPH_OUTCOMES"] = "0"

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

from hub import Hub, Task  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


class FakeMsg:
    """最小 NATS 消息桩：只需要 data 与 respond。"""

    def __init__(self, payload: dict):
        self.data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.reply = "reply.subject"
        self.responses = []

    async def respond(self, data: bytes):
        try:
            self.responses.append(json.loads(data.decode("utf-8")))
        except Exception:
            self.responses.append({"raw": data.decode("utf-8", "replace")})


def make_hub():
    base = Path(tempfile.mkdtemp())
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    return Hub(base, "nats://127.0.0.1:43321")


def add_task(hub, task_id, state, implementer="codex"):
    env = {
        "task_id": task_id,
        "trace_id": "tr-" + task_id,
        "idempotency_key": "idem-" + task_id,
        "created_by": "test",
        "instruction": "x",
        "params": {},
    }
    t = Task(task_id=task_id, envelope=env, trace_id=env["trace_id"],
             idempotency_key=env["idempotency_key"], state=state)
    t.selected_interface = implementer
    hub.tasks[task_id] = t
    return t


def verdict(hub, payload):
    msg = FakeMsg(payload)
    asyncio.run(hub.on_verdict(msg))
    return msg.responses[-1] if msg.responses else {}


def main():
    hub = make_hub()

    # 1. QUEUED 任务判「通过」-> 拒绝
    add_task(hub, "t-queued", "QUEUED")
    r = verdict(hub, {"task_id": "t-queued", "verdict": "通过", "checker": "dsh"})
    check("VG-1 pass on QUEUED rejected", r.get("ok") is False, str(r)[:100])
    check("VG-2 reason=task_not_terminal", r.get("reason") == "task_not_terminal", str(r.get("reason")))
    check("VG-3 reports state", r.get("execution_state") == "QUEUED", str(r.get("execution_state")))
    check("VG-4 lists allowed verdicts",
          set(r.get("allowed_verdicts") or []) == {"不通过", "未核", "作废"},
          str(r.get("allowed_verdicts")))

    # 2. 带注通过 同样被拒
    r2 = verdict(hub, {"task_id": "t-queued", "verdict": "带注通过", "checker": "dsh"})
    check("VG-5 annotated-pass also rejected", r2.get("ok") is False, str(r2)[:80])

    # 3. RUNNING 任务判「通过」-> 拒绝
    add_task(hub, "t-running", "RUNNING")
    r3 = verdict(hub, {"task_id": "t-running", "verdict": "通过", "checker": "dsh"})
    check("VG-6 pass on RUNNING rejected", r3.get("ok") is False, str(r3.get("reason")))

    # 4. 非终态判「不通过」-> 允许
    r4 = verdict(hub, {"task_id": "t-running", "verdict": "不通过", "checker": "dsh"})
    check("VG-7 fail on RUNNING allowed", r4.get("ok") is True, str(r4)[:100])

    # 5. 终态 SUCCEEDED 判通过 -> 允许，且带三层结果字段
    add_task(hub, "t-succ", "SUCCEEDED")
    r5 = verdict(hub, {"task_id": "t-succ", "verdict": "通过", "checker": "dsh"})
    check("VG-8 pass on SUCCEEDED allowed", r5.get("ok") is True, str(r5)[:100])
    rec = r5.get("verdict_record") or {}
    check("VG-9 record carries three_layer key", "three_layer" in rec, str(sorted(rec.keys()))[:120])

    # 6. 非法裁定词仍被拒（原有规则不被新逻辑绕过）
    r6 = verdict(hub, {"task_id": "t-succ", "verdict": "驳回", "checker": "dsh"})
    check("VG-10 legacy alias rejected", r6.get("ok") is False
          and r6.get("reason") == "invalid_verdict_word", str(r6.get("reason")))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
