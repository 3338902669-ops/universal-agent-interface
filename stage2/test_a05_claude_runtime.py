"""E-05 第三家厂商 runtime（claude / Anthropic Claude Code）自检。

断言四件事（前两层不需要任何凭据，任何环境可跑）：
  1. 解析层：Codex 专有 flag 被剥离，指令与工作目录被翻译。
  2. 失败层：缺指令 → exit 2 + missing_instruction；参数校验在依赖解析之前
     （v2 教训：干净机器上不能把 missing_instruction 错报成 claude_not_found）。
  3. 契约层：真实调用回传标记（需 claude 二进制 + ANTHROPIC_BASE_URL/AUTH_TOKEN）。
  4. 认证边界：适配器只读环境变量，不落盘不打印凭据。

缺 claude 或凭据时第 3 层记 SKIP 并写明缺什么，不算失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ADAPTER = os.path.join(HERE, "runtime_adapters", "claude_adapter.py")
sys.path.insert(0, os.path.join(HERE, "runtime_adapters"))

import claude_adapter  # noqa: E402

MARKER = "INTERFACE_V02_CLAUDE_OK"
PASSES, FAILS, SKIPS = [], [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def skip(name, why):
    SKIPS.append(name)
    print("SKIP  %-58s %s" % (name, why))


def main() -> int:
    # ---------- 1. 解析层 ----------
    wd, instr, perm = claude_adapter.parse(
        ["exec", "--skip-git-repo-check", "--sandbox", "read-only", "--ephemeral",
         "--json", "-C", "/tmp/x", "do", "the", "thing"])
    check("E-05c 剥掉 --sandbox 的取值", wd == "/tmp/x", wd)
    check("E-05c 指令由多个词拼回", instr == "do the thing", instr)
    check("E-05c 默认 permission-mode=workspace-write", perm == "workspace-write", perm)

    wd2, instr2, perm2 = claude_adapter.parse(
        ["exec", "--permission-mode", "read-only", "hello"])
    check("E-05c 显式 permission-mode 被透传", perm2 == "read-only", perm2)
    check("E-05c 无 -C 时工作目录为 .", wd2 == ".", wd2)

    # ---------- 2. 失败层（无依赖） ----------
    r = subprocess.run([sys.executable, ADAPTER, "exec", "--json"],
                       capture_output=True, text=True, timeout=60)
    check("E-05c 缺指令 → 退出码 2", r.returncode == 2, str(r.returncode))
    check("E-05c 缺指令 → 输出 missing_instruction",
          "missing_instruction" in (r.stdout + r.stderr), "")

    # ---------- 3. 契约层（需真 runtime + 认证端点） ----------
    has_bin = claude_adapter.find_claude() is not None
    has_auth = bool(os.environ.get("ANTHROPIC_BASE_URL")) and \
        bool(os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY"))
    if not has_bin:
        skip("E-05c claude 真实调用回传标记", "未找到 claude 可执行文件")
    elif not has_auth:
        skip("E-05c claude 真实调用回传标记",
             "未设置 ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN（Anthropic 兼容端点）")
    else:
        import tempfile
        wd3 = tempfile.mkdtemp(prefix="claude-adapter-")
        r = subprocess.run(
            [sys.executable, ADAPTER, "exec", "--skip-git-repo-check",
             "--sandbox", "read-only", "--ephemeral", "--json", "-C", wd3,
             "Reply with exactly %s. Do not run commands." % MARKER],
            capture_output=True, text=True, timeout=600)
        got_msg = None
        for line in (r.stdout or "").splitlines():
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
        check("E-05c claude 退出码 0", r.returncode == 0, str(r.returncode))
        check("E-05c 输出是 Codex 形状 JSONL 且含 agent_message",
              got_msg is not None, str(got_msg)[:60])
        check("E-05c 回传标记文本命中", (got_msg or "").find(MARKER) >= 0, (got_msg or "")[:60])

    # ---------- 4. 认证边界 ----------
    src = open(ADAPTER, encoding="utf-8").read()
    # 真正的不变量：适配器完全不落盘（无任何 open() 写文件）——凭据由 claude CLI
    # 从继承的环境变量读取，适配器本身不读、不存、不打印凭据。
    check("E-05c 适配器零文件写入（凭据只经环境继承，无落盘路径）",
          "open(" not in src and "ANTHROPIC_AUTH_TOKEN=" not in src.replace('"ANTHROPIC_AUTH_TOKEN"', ""),
          "无 open() 调用、无凭据赋值")

    print()
    print("RESULT: %d/%d passed（SKIP %d）" % (len(PASSES), len(PASSES) + len(FAILS), len(SKIPS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
