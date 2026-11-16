"""xiaoyu（Grok 席位）文件信箱适配器。

## 为什么是这个形态，而不是 MCP / CLI

权威事实（图谱 Entity「Grok Bot」「小羽」+ 本机实测 2026-11-17）：

- Grok Bot 本体没有 CLI、不监听任何端口（实测 9 个 Electron 进程均无监听）
- 它的调度通道就是文件信箱：cloud-outbox 收单、cloud-inbox 回件，cron 每 15 分钟轮询
- 唤醒靠 schtasks /Run /TN MuseWakeGrok -> muse-bridge/wake-grok.ps1（该脚本当前缺失）
- 它不只是接单方：2026-11-15 它反过来给 dsh 派过单（cloud-inbox/xiaoyu-20261115-*.md）

所以正确的接入方式是文件信箱：
interface 把任务写成任务卡放进 cloud-outbox，等它写回执，再读回执。

## interface.py 发出的 argv

    <runtime> exec --skip-git-repo-check --sandbox read-only --ephemeral --json -C <workdir> <instruction>

本适配器取 instruction 与 workdir，其余忽略。

## 流程

1. 生成 task_id（可用 XIAOYU_TASK_ID 指定，便于外部关联）
2. 写 cloud-outbox/TASK-xiaoyu-<task_id>.md（格式对齐既有 TASK-GROKBOT-*）
3. 若 MuseWakeGrok 的脚本存在则触发一次唤醒；不存在则如实记录，等 cron 轮询
4. 轮询回执：cloud-outbox/RE-*<task_id>*.md 或 cloud-inbox/*xiaoyu*<task_id>*.md
5. 拿到回执 -> 以 codex 形状 JSONL 输出，text 为回执正文
6. 超时 -> 输出 xiaoyu_no_receipt，退出码 124（告知上层：不是失败，是没回件）

## 环境变量

- XIAOYU_ROOT / XIAOYU_OUTBOX / XIAOYU_INBOX：信箱目录
- XIAOYU_TASK_ID：指定 task_id
- XIAOYU_TIMEOUT_S：等回执秒数，默认 1200（覆盖 cron 15 分钟窗口）
- XIAOYU_POLL_S：轮询间隔，默认 20
- XIAOYU_NO_WAKE=1：不尝试唤醒
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

DEFAULT_ROOT = Path("C:/Users/33389/.shared")
WAKE_TASK = "MuseWakeGrok"
WAKE_SCRIPT = Path("C:/Users/33389/.shared/muse-bridge/wake-grok.ps1")

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


def card_text(task_id, instruction, workdir, receipt_name):
    lines = [
        "# 任务卡：UAI 中台派单（xiaoyu 席位）",
        "",
        "- 来源：UAI 中台（4223）interface 派发，经文件信箱转交",
        "- 席位：xiaoyu（小羽 / Grok 席位）",
        "- task_id：" + chr(96) + task_id + chr(96),
        "- 工作目录：" + chr(96) + workdir + chr(96),
        "- 类型：按 instruction 内容自行判断；默认只读",
        "",
        "## 任务正文",
        "",
        instruction,
        "",
        "## 回执",
        "",
        "请写 cloud-outbox/" + receipt_name + "（文件名必须含上面的 task_id）。",
        "逐条作答，每条标 E1/E2/E3/E4；拿不到的写 UNKNOWN，不猜。",
        "",
        "## 边界",
        "",
        "- 只读或按 instruction 明确授权；不外发、不部署、不改他人文件",
        "- 不写图谱，不写 CURRENT-TASK.md",
        "",
    ]
    return chr(10).join(lines)


def write_card(outbox, task_id, instruction, workdir):
    outbox.mkdir(parents=True, exist_ok=True)
    receipt_name = "RE-xiaoyu-" + task_id + ".md"
    p = outbox / ("TASK-xiaoyu-" + task_id + ".md")
    p.write_text(card_text(task_id, instruction, workdir, receipt_name), encoding="utf-8")
    return p


def try_wake():
    """返回唤醒结果描述。脚本缺失时不假装成功。"""
    if os.environ.get("XIAOYU_NO_WAKE") == "1":
        return "skipped_by_env"
    if not WAKE_SCRIPT.is_file():
        return "wake_script_missing:" + str(WAKE_SCRIPT)
    try:
        r = subprocess.run(["schtasks", "/Run", "/TN", WAKE_TASK],
                           capture_output=True, timeout=30)
        return "wake_ok" if r.returncode == 0 else "wake_failed:%d" % r.returncode
    except Exception as exc:
        return "wake_error:" + str(exc)[:120]


def find_receipt(outbox, inbox, task_id):
    """在 outbox 与 inbox 里找含 task_id 的回执。-> (path, text) 或 None"""
    for d in (outbox, inbox):
        if not d.is_dir():
            continue
        try:
            items = sorted(d.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            continue
        for p in items:
            if not p.is_file():
                continue
            n = p.name
            if task_id not in n:
                continue
            if not (n.startswith("RE-") or n.startswith("xiaoyu-")):
                continue
            try:
                return p, p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return None


def main(argv):
    workdir, instruction = parse(argv)
    if not instruction:
        emit("error", message="missing_instruction")
        return 2

    root = Path(os.environ.get("XIAOYU_ROOT") or DEFAULT_ROOT)
    outbox = Path(os.environ.get("XIAOYU_OUTBOX") or (root / "cloud-outbox"))
    inbox = Path(os.environ.get("XIAOYU_INBOX") or (root / "cloud-inbox"))
    task_id = os.environ.get("XIAOYU_TASK_ID") or uuid.uuid4().hex[:12]
    timeout_s = float(os.environ.get("XIAOYU_TIMEOUT_S", "1200"))
    poll_s = float(os.environ.get("XIAOYU_POLL_S", "20"))

    try:
        card = write_card(outbox, task_id, instruction, workdir)
    except Exception as exc:
        emit("error", message="card_write_failed:" + str(exc)[:150])
        return 1

    wake = try_wake()

    emit("thread.started", thread_id="xiaoyu-mailbox")
    emit("turn.started")
    emit("mailbox.note", task_id=task_id, card=str(card), wake=wake,
         note="xiaoyu 无 CLI/无端口，走 cloud-outbox 收单、cloud-inbox 回件，cron 15 分钟轮询")

    deadline = time.time() + timeout_s
    found = None
    while time.time() < deadline:
        found = find_receipt(outbox, inbox, task_id)
        if found:
            break
        time.sleep(poll_s)

    if found is None:
        emit("item.completed",
             item={"id": "item_0", "type": "agent_message",
                   "text": "xiaoyu_no_receipt: 任务卡已投递 " + card.name
                           + "，等待 %.0fs 未收到含 " % timeout_s + task_id + " 的回执。"})
        emit("turn.completed", exit_code=124, receipt=None, wake=wake)
        return 124

    path, text = found
    emit("item.completed",
         item={"id": "item_0", "type": "agent_message", "text": text})
    emit("turn.completed", exit_code=0, receipt=str(path), wake=wake)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
