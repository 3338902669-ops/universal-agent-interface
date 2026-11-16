"""天枢（rivet）适配器：把 Codex 风格 argv 翻译成 rivet headless one-shot。

背景：本机真实团队是 DSH / Codex / 天枢 / GrokBot。DSH 与 Codex 的 argv 与
`prototype/interface.py` 发出的形状同构，天枢不是——它用 `rivet -p "<prompt>" --json`，
没有 `-C`，cwd 靠进程继承。本适配器做这一层转译。

interface.py 发出的 argv：

    <runtime> exec --skip-git-repo-check --sandbox read-only --ephemeral --json -C <workdir> <instruction>

rivet 实际调用（实测 2026-11-16）：

    rivet -p "<instruction>" --json --new      （cwd = workdir）

`--new` 必须有：否则 rivet 会 `-c` 续接同 cwd 的上一个会话，任务之间上下文串味。

rivet 输出是**单个 JSON 对象**（不是 JSONL）：

    {"success": true, "text": "...", "usage": {...}}

本适配器把它摊平成 Codex 的 JSONL 形状，保持上层不变。

环境陷阱（本机实测）：
- rivet 需要 DEEPSEEK_API_KEY；DSH 起的子进程不继承 HKCU 环境，
  所以这里显式从注册表读一次注入子进程，**不写入任何文件、不打印**。
- rivet 在未授信项目会打印 hooks 警告到 stderr，属正常噪音，不影响 rc。

用法：
    python tianshu_adapter.py exec --json -C . "Reply with exactly OK"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

VALUE_FLAGS = {"-C", "--cd", "--sandbox", "--output-last-message", "--permission-mode"}
BOOL_FLAGS = {"exec", "--json", "--ephemeral", "--skip-git-repo-check", "--stream-json"}


def find_rivet() -> str | None:
    # 显式指定时严格尊重：指定的路径不存在就报 rivet_not_found，
    # 不静默回退到 PATH 上另一个 rivet（否则"我配了 A 却在跑 B"极难排查）。
    env = os.environ.get("TIANSHU_RIVET")
    if env:
        return env if os.path.exists(env) else None
    for name in ("rivet.cmd", "rivet.exe", "rivet"):
        p = shutil.which(name)
        if p:
            return p
    default = os.path.join(os.environ.get("APPDATA", ""), "npm", "rivet.cmd")
    return default if os.path.exists(default) else None


def _inject_api_key(env: dict) -> None:
    """rivet 需要 DEEPSEEK_API_KEY；本机它只在 HKCU 注册表里。"""
    if env.get("DEEPSEEK_API_KEY"):
        return
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")
        if v:
            env["DEEPSEEK_API_KEY"] = v
    except Exception:
        # 非 Windows、或键不存在：交给 rivet 自己报错
        pass


def parse(argv: list[str]) -> tuple[str, str, bool]:
    """→ (workdir, instruction, skip_permissions)"""
    workdir = "."
    instruction_parts: list[str] = []
    skip = os.environ.get("TIANSHU_SKIP_PERMISSIONS", "1") == "1"

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
        if a == "--dangerously-skip-permissions":
            skip = True
            i += 1
            continue
        if a in BOOL_FLAGS or a.startswith("-"):
            i += 1
            continue
        instruction_parts.append(a)
        i += 1

    return workdir, " ".join(instruction_parts).strip(), skip


def emit(typ: str, **fields) -> None:
    payload = {"type": typ}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str]) -> int:
    # 先解析再查依赖：missing_instruction 是参数契约错误，
    # 不该因为「没装 rivet」被错报成 rivet_not_found。
    workdir, instruction, skip = parse(argv)
    if not instruction:
        emit("error", message="missing_instruction")
        return 2

    rivet = find_rivet()
    if not rivet:
        emit("error", message="rivet_not_found")
        return 127

    env = os.environ.copy()
    env.setdefault("HOME", os.path.expanduser("~"))
    _inject_api_key(env)

    cmd = [rivet, "-p", instruction, "--json", "--new"]
    if skip:
        cmd.insert(1, "--dangerously-skip-permissions")

    timeout_s = float(os.environ.get("TIANSHU_TIMEOUT_S", "600"))
    try:
        completed = subprocess.run(
            cmd,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        emit("error", message="rivet_timeout")
        return 124
    except Exception as e:
        emit("error", message=str(e)[:200])
        return 1

    out = completed.stdout.decode("utf-8", "replace").strip()
    err = completed.stderr.decode("utf-8", "replace").strip()

    # rivet --json 是单个对象；容错解析（可能夹带日志行）
    text = ""
    success = None
    usage = None
    parsed = None
    try:
        parsed = json.loads(out)
    except Exception:
        for line in reversed(out.splitlines()):
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                parsed = json.loads(line)
                break
            except Exception:
                continue

    if isinstance(parsed, dict):
        text = str(parsed.get("text") or "")
        success = parsed.get("success")
        usage = parsed.get("usage")
    else:
        text = out

    emit("thread.started", thread_id="tianshu-adapter")
    emit("turn.started")
    emit("item.completed",
         item={"id": "item_0", "type": "agent_message", "text": text})
    emit("turn.completed",
         exit_code=completed.returncode,
         success=success,
         usage=usage,
         stderr_tail=err[-400:])

    if completed.returncode != 0:
        return completed.returncode
    if success is False:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))