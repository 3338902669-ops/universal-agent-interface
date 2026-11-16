"""E-XYA: xiaoyu 唤醒适配器（comms-v2 legacy task 通道）契约测试。

不依赖真实 GrokBot 的部分用临时目录；真实唤醒默认跳过（会在 GUI 里发消息），
设 XIAOYU_E2E=1 才跑。
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
    _, i2 = xa.parse(["exec", "-C", ".", "写一个", "hello world", "程序"])
    check("XYA-3 multiword intact", i2 == "写一个 hello world 程序", i2)

    # ---- 2. 任务单格式（运营给定，逐字核对）----
    spec = xa.task_spec("abc123", "只回复 OK", Path("C:/x/comms-tasks/abc123.txt"))
    lines = spec.rstrip().splitlines()
    check("XYA-4 spec has 2 lines", len(lines) == 2, str(len(lines)))
    check("XYA-5 spec head format",
          lines[0] == "comms-v2 v0.2 legacy task, task abc123: 只回复 OK", lines[0][:70])
    check("XYA-6 spec tail instruction",
          lines[1].startswith("When done, write the result as plain text into "), lines[1][:70])
    check("XYA-7 spec tail names result file",
          "abc123.txt" in lines[1], "")
    check("XYA-8 spec tail no-mail clause",
          "do not send a mail reply" in lines[1], "")

    # ---- 3. 缺指令 ----
    r0 = subprocess.run([sys.executable, str(ADAPTER), "exec", "--json", "-C", "."],
                        capture_output=True, text=True, timeout=60)
    check("XYA-9 missing instruction rc=2", r0.returncode == 2, str(r0.returncode))
    check("XYA-10 missing instruction payload",
          any(p.get("message") == "missing_instruction" for p in jsonl(r0.stdout)), "")

    # ---- 4. 不唤醒 + 无结果 -> 124（诚实超时）----
    tmp = Path(tempfile.mkdtemp())
    env = {**os.environ,
           "XIAOYU_BRIDGE": str(tmp / "bridge"),
           "XIAOYU_TASKS_DIR": str(tmp / "tasks"),
           "XIAOYU_TASK_ID": "nomock1",
           "XIAOYU_NO_WAKE": "1",
           "XIAOYU_TIMEOUT_S": "2",
           "XIAOYU_POLL_S": "0.3"}
    r1 = subprocess.run([sys.executable, str(ADAPTER), "exec", "--json", "-C", ".", "没人应答"],
                        capture_output=True, text=True, timeout=90, env=env)
    check("XYA-11 timeout rc=124", r1.returncode == 124, str(r1.returncode))
    texts = [p.get("item", {}).get("text", "") for p in jsonl(r1.stdout)
             if p.get("type") == "item.completed"]
    check("XYA-12 timeout says no result",
          texts and "xiaoyu_no_result" in texts[0], str(texts[:1])[:80])
    spec_file = tmp / "bridge" / "wake-msg.txt"
    check("XYA-13 spec written to bridge", spec_file.exists(), str(spec_file))
    if spec_file.exists():
        body = spec_file.read_text(encoding="utf-8")
        check("XYA-14 spec id matches", "task nomock1:" in body, "")

    # ---- 5. 模拟结果文件出现 -> 适配器读到 ----
    tmp2 = Path(tempfile.mkdtemp())
    tasks2 = tmp2 / "tasks"
    tasks2.mkdir(parents=True)
    env2 = {**os.environ,
            "XIAOYU_BRIDGE": str(tmp2 / "bridge"),
            "XIAOYU_TASKS_DIR": str(tasks2),
            "XIAOYU_TASK_ID": "mock01",
            "XIAOYU_NO_WAKE": "1",
            "XIAOYU_TIMEOUT_S": "10",
            "XIAOYU_POLL_S": "0.3"}
    p = subprocess.Popen([sys.executable, str(ADAPTER), "exec", "--json", "-C", ".", "模拟回件"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                         encoding="utf-8", env=env2)
    target = tasks2 / "mock01.txt"
    for _ in range(60):
        if (tmp2 / "bridge" / "wake-msg.txt").exists():
            break
        time.sleep(0.2)
    # 带 BOM 写，验证 BOM 处理
    target.write_bytes(b"\xef\xbb\xbfMOCK_OK")
    out, _ = p.communicate(timeout=40)
    check("XYA-15 rc=0 when result appears", p.returncode == 0, str(p.returncode))
    t2 = [x.get("item", {}).get("text", "") for x in jsonl(out)
          if x.get("type") == "item.completed"]
    check("XYA-16 BOM stripped from result", t2 and t2[0] == "MOCK_OK", str(t2[:1])[:60])

    # ---- 6. 触发方式：脚本缺失时应如实报错 ----
    tmp3 = Path(tempfile.mkdtemp())
    env3 = {**os.environ,
            "XIAOYU_BRIDGE": str(tmp3 / "bridge"),
            "XIAOYU_TASKS_DIR": str(tmp3 / "tasks"),
            "XIAOYU_TASK_ID": "trig01",
            "XIAOYU_TRIGGER": "script",
            "XIAOYU_TIMEOUT_S": "2",
            "XIAOYU_POLL_S": "0.3"}
    r3 = subprocess.run([sys.executable, str(ADAPTER), "exec", "--json", "-C", ".", "触发测试"],
                        capture_output=True, text=True, timeout=90, env=env3)
    notes = [p for p in jsonl(r3.stdout) if p.get("type") == "wake.note"]
    check("XYA-17 trigger reported", bool(notes), str(notes[:1])[:90])
    check("XYA-18 trigger failure not disguised as ok",
          bool(notes) and notes[0].get("ok") is False, str(notes[0].get("trigger"))[:80] if notes else "")

    # ---- 7. 真实唤醒（可选，会在 GUI 里发消息）----
    if os.environ.get("XIAOYU_E2E") == "1":
        env4 = {**os.environ, "XIAOYU_TIMEOUT_S": "180", "XIAOYU_POLL_S": "3"}
        r4 = subprocess.run([sys.executable, str(ADAPTER), "exec", "--json", "-C", ".",
                             "只回复 XIAOYU_TEST_OK，不调用工具、不读写其他文件。"],
                            capture_output=True, text=True, timeout=240, env=env4)
        t4 = [x.get("item", {}).get("text", "") for x in jsonl(r4.stdout)
              if x.get("type") == "item.completed"]
        check("XYA-19 real wake returned result",
              r4.returncode == 0 and t4 and "XIAOYU_TEST_OK" in t4[0],
              str(t4[:1])[:80])
    else:
        print("SKIP  XYA-19 真实唤醒（设 XIAOYU_E2E=1 才跑，会在 GUI 里发消息）")

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
