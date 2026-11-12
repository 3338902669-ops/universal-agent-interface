"""第三 runtime 适配器：把 Codex 风格的 argv 翻译成 Claude Code headless。

背景（待完善项 E-05 的第三家厂商实证）：v2 已接 codex（原生）与 dsh（适配），
本适配器接入 claude（Anthropic Claude Code），使"跨厂商/异构"有三家实证。

它接受的 argv 与 prototype/interface.py 发给 runtime 的完全一致：

    <runtime> exec --skip-git-repo-check --sandbox read-only --ephemeral --json -C <workdir> <instruction>

Codex 专有 flag 在这里被忽略/翻译（Claude Code 的非交互模式是 `claude -p <prompt>
--output-format json`），输出统一成 Codex 的 JSONL 形状（item.completed /
agent_message），便于上层不变。

认证：Claude Code 通过环境变量认证（ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN +
ANTHROPIC_MODEL，指向任意 Anthropic 兼容端点——本仓验证用的是 DeepSeek 的
/anthropic 端点）。凭据只从环境读取，不落盘、不打印。

用法：
    python claude_adapter.py exec --json -C . "Reply with exactly OK"
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


def find_claude() -> str | None:
    env = os.environ.get("CLAUDE_EXECUTABLE")
    if env and os.path.exists(env):
        return env
    for name in ("claude.cmd", "claude.exe", "claude"):
        p = shutil.which(name)
        if p:
            return p
    npm = os.environ.get("APPDATA")
    if npm:
        default = os.path.join(npm, "npm", "claude.cmd")
        if os.path.exists(default):
            return default
    return None


def parse(argv: list[str]) -> tuple[str | None, str, str]:
    """→ (workdir, instruction, permission_mode)。逻辑与 dsh_adapter.parse 一致。"""
    workdir = "."
    instruction_parts: list[str] = []
    perm = os.environ.get("CLAUDE_PERMISSION_MODE", "workspace-write")

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


def _claude_argv(claude: str, workdir: str, instruction: str, perm: str) -> list[str]:
    """Claude Code 非交互调用：-p <prompt> --output-format json --max-turns 1。

    permission_mode 映射（保守）：read-only 不加权限跳过 flag，其余由调用方
    通过 CLAUDE_CODE 的权限配置决定——本适配器不自动加
    --allow-dangerously-skip-permissions（安全边界）。
    """
    argv = [claude, "-p", instruction, "--output-format", "json", "--max-turns", "1"]
    if perm == "read-only":
        argv.append("--allowedTools")
        argv.append("Read")               # 只读工具白名单
    return argv


def main(argv: list[str]) -> int:
    # 参数校验必须在依赖解析之前（v2 教训：缺指令不该被 dsh_not_found 顶替）
    workdir, instruction, perm = parse(argv)
    if not instruction:
        print(json.dumps({"type": "error", "message": "missing_instruction"}), flush=True)
        return 2

    claude = find_claude()
    if not claude:
        print(json.dumps({"type": "error", "message": "claude_not_found"}), flush=True)
        return 127

    cmd = _claude_argv(claude, workdir, instruction, perm)
    try:
        completed = subprocess.run(
            cmd, cwd=workdir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=600,
        )
    except Exception as e:
        print(json.dumps({"type": "error", "message": str(e)[:200]}), flush=True)
        return 1

    blob = completed.stdout.decode("utf-8", "replace").strip()
    err = completed.stderr.decode("utf-8", "replace").strip()

    # Claude Code --output-format json 是单个 JSON 对象（result 字段 = 最终文本）
    result_text = None
    is_error = False
    try:
        obj = json.loads(blob)
        result_text = obj.get("result")
        is_error = bool(obj.get("is_error"))
    except Exception:
        result_text = blob if blob else None

    if completed.returncode != 0 or is_error:
        # 通道失败：如实报错（如未登录、api_error），不伪装成能力失败
        print(json.dumps({"type": "thread.started", "thread_id": "claude-adapter"}, ensure_ascii=False))
        print(json.dumps({"type": "turn.started"}, ensure_ascii=False))
        print(json.dumps({"type": "item.completed",
                          "item": {"id": "item_0", "type": "agent_message",
                                   "text": "claude_channel_failure: %s"
                                           % ((result_text or err or "unknown")[:200])}},
                         ensure_ascii=False))
        print(json.dumps({"type": "turn.completed", "exit_code": completed.returncode,
                          "stderr_tail": err[-400:]}, ensure_ascii=False))
        return completed.returncode or 1

    # 统一成 Codex 的 JSONL 形状
    print(json.dumps({"type": "thread.started", "thread_id": "claude-adapter"}, ensure_ascii=False))
    print(json.dumps({"type": "turn.started"}, ensure_ascii=False))
    print(json.dumps({"type": "item.completed",
                      "item": {"id": "item_0", "type": "agent_message", "text": result_text or ""}},
                     ensure_ascii=False))
    print(json.dumps({"type": "turn.completed", "exit_code": completed.returncode,
                      "stderr_tail": err[-400:]}, ensure_ascii=False))
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
