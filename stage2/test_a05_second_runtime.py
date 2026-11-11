"""E-05 自检：第二种真实 runtime（dsh）挂到同一套 interface 协议。

断言三件事：
  1. 解析层：Codex 专有 flag 被正确剥离，指令与工作目录被翻译（不需要网络）。
  2. 契约层：同一份 Codex 风格 argv，dsh 适配器能接受并回传标记文本（需要 DEEPSEEK_API_KEY）。
  3. 失败层：缺指令时明确报错，不静默成功。

无凭据时第 2 条记 SKIP，不算失败——但会打印说明。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(HERE, "runtime_adapters", "dsh_adapter.py")
sys.path.insert(0, os.path.join(HERE, "runtime_adapters"))

import dsh_adapter  # noqa: E402

MARKER = "INTERFACE_V02_RUNTIME_OK"
PASSES, FAILS, SKIPS = [], [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def skip(name, why):
    SKIPS.append(name)
    print("SKIP  %-58s %s" % (name, why))


def main() -> int:
    # ---------- 1. 解析层 ----------
    wd, instr, perm = dsh_adapter.parse(
        ["exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral",
         "--json", "-C", "/tmp/x", "do", "the", "thing"])
    check("E-05 剥掉 --sandbox 的取值", wd == "/tmp/x", wd)
    check("E-05 指令由多个词拼回", instr == "do the thing", instr)
    check("E-05 默认 permission-mode=workspace-write", perm == "workspace-write", perm)

    wd2, instr2, perm2 = dsh_adapter.parse(
        ["exec", "--permission-mode", "read-only", "hello"])
    check("E-05 显式 permission-mode 被透传", perm2 == "read-only", perm2)
    check("E-05 无 -C 时工作目录为 .", wd2 == ".", wd2)

    # ---------- 2. 失败层 ----------
    r = subprocess.run([sys.executable, ADAPTER, "exec", "--json"],
                       capture_output=True, text=True, timeout=60)
    check("E-05 缺指令 → 退出码 2", r.returncode == 2, str(r.returncode))
    check("E-05 缺指令 → 输出 missing_instruction",
          "missing_instruction" in (r.stdout + r.stderr), "")

    # ---------- 3. 契约层（需真 runtime） ----------
    if not os.environ.get("DEEPSEEK_API_KEY"):
        skip("E-05 dsh 真实调用回传标记", "未设置 DEEPSEEK_API_KEY")
    else:
        wd3 = tempfile.mkdtemp(prefix="dsh-adapter-")
        r = subprocess.run(
            [sys.executable, ADAPTER, "exec", "--skip-git-repo-check",
             "--sandbox", "read-only", "--ephemeral", "--json", "-C", wd3,
             "Reply with exactly %s. Do not run commands." % MARKER],
            capture_output=True, text=True, timeout=600)
        blob = r.stdout
        got_msg = None
        for line in blob.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            item = ev.get("item") or {}
            if ev.get("type") == "item.completed" and item.get("type") == "agent_message":
                got_msg = item.get("text")
        check("E-05 dsh 退出码 0", r.returncode == 0, str(r.returncode))
        check("E-05 输出是 Codex 形状的 JSONL 且含 agent_message",
              got_msg is not None, str(got_msg)[:60])
        check("E-05 回传标记文本命中", (got_msg or "").find(MARKER) >= 0, (got_msg or "")[:60])

    print()
    print("RESULT: %d/%d passed（SKIP %d）" % (len(PASSES), len(PASSES) + len(FAILS), len(SKIPS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
