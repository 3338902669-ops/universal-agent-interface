"""xiaoyu（Grok 席位）唤醒适配器 —— comms-v2 legacy task 通道。

## 真实链路（2026-11-17 实测打通，E1）

    interface.py argv
      -> 写 .shared/muse-bridge/wake-msg.txt（comms-v2 格式任务单）
      -> 运行 wake-grok.ps1（MuseWakeGrok 指向它）
           UIA 恢复并置前 Grok Bot 窗口
           找 Edit(name=提示词) 写入任务单
           Enter 发送
      -> Grok Bot 自己执行
      -> 写 cloud-outbox/comms-tasks/<task_id>.txt
      -> 本适配器读到该文件，作为结果返回

实测：00:36:42 发送 -> 00:36:56 结果落盘（14 秒）。

## 任务单格式（由运维给定，照抄）

    comms-v2 v0.2 legacy task, task <task_id>: <instruction>
    When done, write the result as plain text into <path>. Writing the file completes the task; do not send a mail reply.

## 历史背景（为什么不再用 TASK-*.md + RE-*.md）

早期派单走 cloud-outbox/TASK-GROKBOT-*.md 再等 RE-*.md，实测间隔约 39 分钟，
依赖 cron 轮询。comms-v2 通道是唤醒式，秒级，且结果路径由任务单指定。

## 环境变量

- XIAOYU_BRIDGE        默认 C:/Users/33389/.shared/muse-bridge
- XIAOYU_TASKS_DIR     默认 C:/Users/33389/.shared/cloud-outbox/comms-tasks
- XIAOYU_TASK_ID       指定 task_id（默认短 uuid）
- XIAOYU_TIMEOUT_S     等结果秒数，默认 300
- XIAOYU_POLL_S        轮询间隔，默认 2
- XIAOYU_NO_WAKE=1     只写任务单不触发唤醒（调试用）
- XIAOYU_TRIGGER       auto|script|schtasks，默认 auto
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

BRIDGE = Path("C:/Users/33389/.shared/muse-bridge")
TASKS_DIR = Path("C:/Users/33389/.shared/cloud-outbox/comms-tasks")
WAKE_SCRIPT = BRIDGE / "wake-grok.ps1"
WAKE_MSG = BRIDGE / "wake-msg.txt"
WAKE_TASK = "MuseWakeGrok"
PS51 = "C:/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"

VALUE_FLAGS = {"-C", "--cd", "--sandbox", "--output-last-message", "--permission-mode"}
BOOL_FLAGS = {"exec", "--json", "--ephemeral", "--skip-git-repo-check", "--stream-json"}


def parse(argv):
    """-> (workdir, instruction)"""
    workdir = "."
    parts = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "--cd") and i + 1 < len(argv):
            workdir = argv[i + 1]
            i += 2
            continue
        if a in VALUE_FLAGS:
            i += 2 if i + 1 < len(argv) else 1
            continue
        if a in BOOL_FLAGS or a.startswith("-"):
            i += 1
            continue
        parts.append(a)
        i += 1
    return workdir, " ".join(parts).strip()


def emit(typ, **fields):
    payload = {"type": typ}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def task_spec(task_id, instruction, result_path):
    """comms-v2 legacy task 任务单（运营给定格式，勿改）"""
    head = "comms-v2 v0.2 legacy task, task " + task_id + ": " + instruction
    tail = ("When done, write the result as plain text into " + str(result_path)
            + ". Writing the file completes the task; do not send a mail reply.")
    return head + chr(10) + tail + chr(10)


def write_spec(msg_file, text):
    msg_file.parent.mkdir(parents=True, exist_ok=True)
    msg_file.write_text(text, encoding="utf-8")
    return msg_file


def trigger(mode, bridge=None):
    """返回 (ok, detail)。auto = 先试脚本直调，失败回退 schtasks。

    bridge 必须显式传入：否则测试/多实例场景会误用模块常量指向的真实路径。
    """
    if mode == "none":
        return False, "skipped_by_env"
    wake_script = (Path(bridge) if bridge else BRIDGE) / "wake-grok.ps1"
    errors = []
    if mode in ("auto", "script") and wake_script.is_file():
        try:
            r = subprocess.run(
                [PS51, "-NoProfile", "-ExecutionPolicy", "Bypass",
                 "-File", str(wake_script)],
                capture_output=True, timeout=180,
            )
            if r.returncode == 0:
                return True, "script_ok"
            tail = (r.stdout or b"").decode("utf-8", "replace")[-200:]
            errors.append("script_rc=%d %s" % (r.returncode, tail[-120:]))
        except Exception as exc:
            errors.append("script_exc:%s" % str(exc)[:100])
    if mode in ("auto", "schtasks"):
        try:
            r = subprocess.run(["schtasks", "/Run", "/TN", WAKE_TASK],
                               capture_output=True, timeout=30)
            if r.returncode == 0:
                return True, "schtasks_ok"
            errors.append("schtasks_rc=%d" % r.returncode)
        except Exception as exc:
            errors.append("schtasks_exc:%s" % str(exc)[:100])
    return False, "; ".join(errors) or "no_trigger_available"


def read_result(path):
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    return raw.decode("utf-8", "replace")


def main(argv):
    workdir, instruction = parse(argv)
    if not instruction:
        emit("error", message="missing_instruction")
        return 2

    bridge = Path(os.environ.get("XIAOYU_BRIDGE") or BRIDGE)
    tasks_dir = Path(os.environ.get("XIAOYU_TASKS_DIR") or TASKS_DIR)
    task_id = os.environ.get("XIAOYU_TASK_ID") or uuid.uuid4().hex[:12]
    timeout_s = float(os.environ.get("XIAOYU_TIMEOUT_S", "300"))
    poll_s = float(os.environ.get("XIAOYU_POLL_S", "2"))
    trigger_mode = os.environ.get("XIAOYU_TRIGGER", "auto")
    if os.environ.get("XIAOYU_NO_WAKE") == "1":
        trigger_mode = "none"

    tasks_dir.mkdir(parents=True, exist_ok=True)
    result_path = tasks_dir / (task_id + ".txt")
    spec = task_spec(task_id, instruction, result_path)

    try:
        msg_file = write_spec(bridge / "wake-msg.txt", spec)
    except Exception as exc:
        emit("error", message="spec_write_failed:" + str(exc)[:150])
        return 1

    emit("thread.started", thread_id="xiaoyu-comms-v2")
    emit("turn.started")

    ok, detail = trigger(trigger_mode, bridge)
    emit("wake.note", task_id=task_id, spec_file=str(msg_file),
         result_path=str(result_path), trigger=detail, ok=ok,
         note="comms-v2 legacy task 通道：写 wake-msg.txt，UIA 注入 Grok Bot，结果落 comms-tasks")

    deadline = time.time() + timeout_s
    content = None
    while time.time() < deadline:
        content = read_result(result_path)
        if content is not None and content.strip():
            break
        time.sleep(poll_s)
        content = None

    if content is None or not content.strip():
        emit("item.completed",
             item={"id": "item_0", "type": "agent_message",
                   "text": "xiaoyu_no_result: 任务单已注入 " + task_id
                           + "（trigger=" + detail + "），等待 %.0fs 未收到 " % timeout_s
                           + str(result_path) + "。"})
        emit("turn.completed", exit_code=124, result_path=str(result_path), trigger=detail)
        return 124

    emit("item.completed",
         item={"id": "item_0", "type": "agent_message", "text": content})
    emit("turn.completed", exit_code=0, result_path=str(result_path), trigger=detail)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
