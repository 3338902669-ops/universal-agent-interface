"""E-XYA: xiaoyu 文件信箱适配器契约测试。

不依赖真实 GrokBot：用临时信箱目录模拟「投卡 -> 回执」全过程。
真实派单默认跳过（需等 cron 轮询）；设 XIAOYU_E2E=1 才跑。
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "runtime_adapters"))
import xiaoyu_adapter as xa  # noqa: E402

ADAPTER = HERE / "runtime_adapters" / "xiaoyu_adapter.py"
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def run_adapter(args, env_extra=None, timeout=60):
    env = os.environ.copy()
    env["XIAOYU_NO_WAKE"] = "1"
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(ADAPTER)] + args,
                          capture_output=True, text=True, timeout=timeout, env=env)


def jsonl(text):
    out = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def main():
    # ---- 1. argv 契约 ----
    wd, instr = xa.parse(["exec", "--skip-git-repo-check", "--sandbox", "read-only",
                          "--ephemeral", "--json", "-C", "D:/work", "do the thing"])
    check("XYA-1 workdir parsed", wd == "D:/work", wd)
    check("XYA-2 instruction parsed", instr == "do the thing", instr)
    _, instr2 = xa.parse(["exec", "-C", ".", "写一个", "hello world", "程序"])
    check("XYA-3 multiword intact", instr2 == "写一个 hello world 程序", instr2)

    # ---- 2. 缺指令 ----
    r0 = run_adapter(["exec", "--json", "-C", "."])
    check("XYA-4 missing instruction rc=2", r0.returncode == 2, str(r0.returncode))
    msgs = [p.get("message") for p in jsonl(r0.stdout)]
    check("XYA-5 missing instruction payload", "missing_instruction" in msgs, str(msgs[:1]))

    # ---- 3. 投卡 + 等回执（模拟 xiaoyu 回件）----
    tmp = Path(tempfile.mkdtemp())
    outbox = tmp / "outbox"
    inbox = tmp / "inbox"
    outbox.mkdir(); inbox.mkdir()
    tid = "testtask01"
    env = {
        "XIAOYU_OUTBOX": str(outbox),
        "XIAOYU_INBOX": str(inbox),
        "XIAOYU_TASK_ID": tid,
        "XIAOYU_TIMEOUT_S": "20",
        "XIAOYU_POLL_S": "0.5",
    }
    # 后台起适配器，再模拟回件
    p = subprocess.Popen([sys.executable, str(ADAPTER), "exec", "--json",
                          "-C", ".", "核实某个状态"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, encoding="utf-8", env={**os.environ, **env})
    # 等任务卡落地
    card = outbox / ("TASK-xiaoyu-" + tid + ".md")
    for _ in range(40):
        if card.exists():
            break
        time.sleep(0.2)
    check("XYA-6 task card written", card.exists(), str(card.name))
    card_txt = card.read_text(encoding="utf-8") if card.exists() else ""
    check("XYA-7 card contains task_id", tid in card_txt, "")
    check("XYA-8 card demands receipt with task_id",
          ("RE-xiaoyu-" + tid) in card_txt, "")
    check("XYA-9 card states read-only boundary",
          "不外发" in card_txt and "不写图谱" in card_txt, "")

    # 模拟 xiaoyu 写回执
    receipt = outbox / ("RE-xiaoyu-" + tid + ".md")
    receipt.write_text("# 回执\n\n1. 状态已核实（E1）\n", encoding="utf-8")
    out, err = p.communicate(timeout=40)
    check("XYA-10 adapter rc=0 on receipt", p.returncode == 0, str(p.returncode))
    items = jsonl(out)
    texts = [i.get("item", {}).get("text", "") for i in items
             if i.get("type") == "item.completed"]
    check("XYA-11 receipt text returned", texts and "状态已核实" in texts[0], str(texts[:1])[:90])
    notes = [i for i in items if i.get("type") == "mailbox.note"]
    check("XYA-12 mailbox note emitted", bool(notes), str(notes[:1])[:100])
    wake_status = str(notes[0].get("wake", "")) if notes else ""
    check("XYA-13 wake status truthful (never claims ok when script missing)",
          bool(notes) and wake_status != "wake_ok"
          and ("missing" in wake_status or "skipped" in wake_status),
          wake_status)

    # ---- 4. 超时 -> rc 124，不假装成功 ----
    tmp2 = Path(tempfile.mkdtemp())
    (tmp2 / "outbox").mkdir(); (tmp2 / "inbox").mkdir()
    r2 = run_adapter(["exec", "--json", "-C", ".", "无人应答的任务"], {
        "XIAOYU_OUTBOX": str(tmp2 / "outbox"),
        "XIAOYU_INBOX": str(tmp2 / "inbox"),
        "XIAOYU_TASK_ID": "nores01",
        "XIAOYU_TIMEOUT_S": "2",
        "XIAOYU_POLL_S": "0.5",
    }, timeout=60)
    check("XYA-14 timeout rc=124", r2.returncode == 124, str(r2.returncode))
    t2 = [i.get("item", {}).get("text", "") for i in jsonl(r2.stdout)
          if i.get("type") == "item.completed"]
    check("XYA-15 timeout says no receipt", t2 and "xiaoyu_no_receipt" in t2[0], str(t2[:1])[:80])

    # ---- 5. inbox 里的回件也能识别（xiaoyu 可能写 cloud-inbox）----
    tmp3 = Path(tempfile.mkdtemp())
    ob3 = tmp3 / "outbox"; ib3 = tmp3 / "inbox"
    ob3.mkdir(); ib3.mkdir()
    (ib3 / "xiaoyu-zz01-reply.md").write_text("inbox 回件正文", encoding="utf-8")
    r3 = run_adapter(["exec", "--json", "-C", ".", "查 inbox"], {
        "XIAOYU_OUTBOX": str(ob3),
        "XIAOYU_INBOX": str(ib3),
        "XIAOYU_TASK_ID": "zz01",
        "XIAOYU_TIMEOUT_S": "6",
        "XIAOYU_POLL_S": "0.3",
    }, timeout=60)
    t3 = [i.get("item", {}).get("text", "") for i in jsonl(r3.stdout)
          if i.get("type") == "item.completed"]
    check("XYA-16 inbox receipt detected", r3.returncode == 0 and t3 and "inbox 回件正文" in t3[0],
          str(t3[:1])[:80])

    # ---- 6. 真实派单（可选）----
    if os.environ.get("XIAOYU_E2E") == "1":
        r4 = run_adapter(["exec", "--json", "-C", ".",
                          "只读核实：UAI 4223 中台当前是否在监听，用一条命令证明。"], {
            "XIAOYU_TIMEOUT_S": "1500",
            "XIAOYU_POLL_S": "30",
        }, timeout=1700)
        check("XYA-17 real dispatch got receipt or honest timeout",
              r4.returncode in (0, 124), str(r4.returncode))
    else:
        print("SKIP  XYA-17 真实派单（设 XIAOYU_E2E=1 才跑，需等 cron 轮询）")

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
