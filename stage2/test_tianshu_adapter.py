"""E-TSA: 天枢（rivet）适配器契约测试。

覆盖：argv 解析契约、缺失指令、rivet 未安装、输出摊平形状。
真实调用默认跳过（会消耗额度）；设 TIANSHU_E2E=1 才跑。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "runtime_adapters"))

import tianshu_adapter as ta  # noqa: E402

PASSES, FAILS = [], []
ADAPTER = HERE / "runtime_adapters" / "tianshu_adapter.py"


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def run(args, env_extra=None, timeout=60):
    env = os.environ.copy()
    env.update(env_extra or {})
    return subprocess.run(
        [sys.executable, str(ADAPTER)] + args,
        capture_output=True, text=True, timeout=timeout, env=env,
    )


def main():
    # ---- 解析契约：interface.py 实际发出的 argv 形状 ----
    argv = ["exec", "--skip-git-repo-check", "--sandbox", "read-only",
            "--ephemeral", "--json", "-C", "D:/work", "do the thing"]
    wd, instr, skip = ta.parse(argv)
    check("TSA-1 workdir extracted", wd == "D:/work", wd)
    check("TSA-2 instruction extracted", instr == "do the thing", instr)
    check("TSA-3 skip-permissions默认开", skip is True, str(skip))

    # codex 专有旗标不得混进 instruction
    wd2, instr2, _ = ta.parse(["exec", "--sandbox", "read-only", "-C", "x", "a b c"])
    check("TSA-4 flags stripped from instruction", instr2 == "a b c", instr2)

    # 多词指令保持完整
    _, instr3, _ = ta.parse(["exec", "-C", ".", "写一个", "hello world", "程序"])
    check("TSA-5 multiword instruction intact", instr3 == "写一个 hello world 程序", instr3)

    # ---- 缺指令 -> rc 2 + 结构化错误 ----
    r = run(["exec", "--json", "-C", "."])
    check("TSA-6 missing instruction rc=2", r.returncode == 2, str(r.returncode))
    first = (r.stdout.strip().splitlines() or [""])[0]
    ok = False
    try:
        ok = json.loads(first).get("message") == "missing_instruction"
    except Exception:
        pass
    check("TSA-7 missing instruction error payload", ok, first[:80])

    # ---- rivet 未安装 -> rc 127（参数已合法时）----
    r2 = run(["exec", "-C", ".", "hello"], {"TIANSHU_RIVET": "C:/nope/rivet.cmd"})
    check("TSA-8 rivet_not_found rc=127", r2.returncode == 127, str(r2.returncode))
    payloads = []
    for line in r2.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                payloads.append(json.loads(line))
            except Exception:
                pass
    check("TSA-9 rivet_not_found payload",
          any(p.get("message") == "rivet_not_found" for p in payloads), str(payloads[:1]))

    # ---- 真实调用（可选）----
    if os.environ.get("TIANSHU_E2E") == "1":
        import tempfile
        work = tempfile.mkdtemp()
        r3 = run(["exec", "--skip-git-repo-check", "--sandbox", "read-only",
                  "--ephemeral", "--json", "-C", work,
                  "Reply with exactly: TSA_E2E_OK"], timeout=300)
        lines = [l for l in r3.stdout.splitlines() if l.strip().startswith("{")]
        kinds = [json.loads(l).get("type") for l in lines]
        check("TSA-10 e2e rc=0", r3.returncode == 0, str(r3.returncode))
        check("TSA-11 e2e emits codex-shaped JSONL",
              kinds[:3] == ["thread.started", "turn.started", "item.completed"],
              str(kinds))
        text = ""
        for l in lines:
            d = json.loads(l)
            if d.get("type") == "item.completed":
                text = d.get("item", {}).get("text", "")
        check("TSA-12 e2e text returned", "TSA_E2E_OK" in text, text[:60])
    else:
        print("SKIP  TSA-10..12 真实调用（设 TIANSHU_E2E=1 才跑，避免消耗额度）")

    print()
    total = len(PASSES) + len(FAILS)
    print("RESULT: %d/%d passed" % (len(PASSES), total))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
