"""第二 runtime 适配器：把 Codex 风格的 argv 翻译成 DeepSeek Harness headless。

背景（待完善项 E-05）：接口契约声称"跨厂商/异构"，但此前只接过 `codex` 一种真实 runtime。
本适配器让 `dsh`（DeepSeek Harness，另一个厂商的 runtime）能挂到同一套 interface 协议后面。

它接受的 argv 与 `prototype/interface.py:927-944` 发给 runtime 的完全一致：

    <runtime> exec --skip-git-repo-check --sandbox read-only --ephemeral --json -C <workdir> <instruction>

Codex 专有的 `--sandbox/--ephemeral/--skip-git-repo-check` 在这里被**忽略**（dsh 的等价物是
`--permission-mode`），指令与工作目录被翻译过去。输出统一成 Codex 的 JSONL 形状，便于上层不变。

用法：
    python dsh_adapter.py exec --json -C . "Reply with exactly OK"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

CODEX_ONLY_FLAGS = {
    "--sandbox", "--ephemeral", "--skip-git-repo-check", "--json",
    "-C", "--cd", "--output-last-message",
}
VALUE_FLAGS = {"--sandbox", "-C", "--cd", "--output-last-message", "--permission-mode"}


def find_dsh() -> str | None:
    env = os.environ.get("DSH_EXECUTABLE")
    if env and os.path.exists(env):
        return env
    for name in ("dsh.cmd", "dsh.exe", "dsh"):
        p = shutil.which(name)
        if p:
            return p
    default = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                           "Programs", "DeepSeek Harness", "resources", "runtime",
                           "cli", "bin", "dsh.cmd")
    return default if os.path.exists(default) else None


def parse(argv: list[str]) -> tuple[str | None, str, str]:
    """→ (workdir, instruction, permission_mode)"""
    workdir = "."
    instruction_parts: list[str] = []
    perm = os.environ.get("DSH_PERMISSION_MODE", "workspace-write")

    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "exec":
            i += 1
            continue
        if a in ("-C", "--cd"):
            workdir = argv[i + 1] if i + 1 < len(argv) else "."
            i += 2
            continue
        if a == "--permission-mode" and i + 1 < len(argv):
            perm = argv[i + 1]
            i += 2
            continue
        if a in CODEX_ONLY_FLAGS:
            if a in VALUE_FLAGS and i + 1 < len(argv):
                i += 2
            else:
                i += 1
            continue
        if a.startswith("-"):
            i += 1
            continue
        instruction_parts.append(a)
        i += 1

    instruction = " ".join(instruction_parts).strip()
    return workdir, instruction, perm


def main(argv: list[str]) -> int:
    # 参数校验必须在依赖解析之前：missing_instruction 是纯解析错误，
    # 不该因为「这台机器没装 dsh」而变成 dsh_not_found（127）。
    # 首版先 find_dsh() 再 parse，导致干净 CI runner 上 CLI 契约测试必然失败——
    # 那是把「环境缺失」错报成「参数契约不成立」。
    workdir, instruction, perm = parse(argv)
    if not instruction:
        print(json.dumps({"type": "error", "message": "missing_instruction"}), flush=True)
        return 2

    dsh = find_dsh()
    if not dsh:
        print(json.dumps({"type": "error", "message": "dsh_not_found"}), flush=True)
        return 127

    cmd = [dsh, "--profile", "headless", "--permission-mode", perm, instruction]
    try:
        completed = subprocess.run(
            cmd, cwd=workdir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
        )
    except Exception as e:
        print(json.dumps({"type": "error", "message": str(e)[:200]}), flush=True)
        return 1

    text = completed.stdout.decode("utf-8", "replace").strip()
    err = completed.stderr.decode("utf-8", "replace").strip()

    # 统一成 Codex 的 JSONL 形状
    print(json.dumps({"type": "thread.started", "thread_id": "dsh-adapter"}, ensure_ascii=False))
    print(json.dumps({"type": "turn.started"}, ensure_ascii=False))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "item_0", "type": "agent_message", "text": text}},
                     ensure_ascii=False))
    print(json.dumps({"type": "turn.completed", "exit_code": completed.returncode,
                      "stderr_tail": err[-400:]}, ensure_ascii=False))
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
