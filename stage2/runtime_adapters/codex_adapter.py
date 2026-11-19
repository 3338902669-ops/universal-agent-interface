"""Codex 适配器：argv 直通 + 凭据注入。

Codex 的 argv 与 `prototype/interface.py` 发出的形状**本来就同构**，所以这个适配器
不做参数翻译——它只补一件事：**凭据注入**。

原因：本机 codex 的 model_provider 是本地 `deepseek` 代理（127.0.0.1:8787），
凭据从 `DEEPSEEK_API_KEY` 读。而 DSH 起的子进程不继承 HKCU 环境，
interface.py 直接 spawn codex.cmd 会拿不到 key → codex 起不来。
适配器从注册表读一次、只放进子进程环境，**不落盘、不打印、不进 supervisor 环境**。

为什么不用"在监督器里设 env"的省事做法：那样 key 会留在常驻进程环境里，
被所有子进程继承，暴露面比"每次调用临时注入"大。

argv 与输出完全透传，Codex 的 JSONL 原样交给上层。

用法（与 interface.py 发出的 argv 一致）：
    python codex_adapter.py exec --skip-git-repo-check --sandbox read-only --ephemeral --json -C <dir> "<instr>"
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys


def find_codex() -> str | None:
    # 显式指定时严格尊重，不静默回退
    env = os.environ.get("CODEX_EXECUTABLE")
    if env:
        return env if os.path.exists(env) else None
    for name in ("codex.cmd", "codex.exe", "codex"):
        p = shutil.which(name)
        if p:
            return p
    default = os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd")
    return default if os.path.exists(default) else None


def inject_credentials(env: dict) -> bool:
    """把 DEEPSEEK_API_KEY 从 HKCU 注入子进程环境。返回是否注入成功。"""
    if env.get("DEEPSEEK_API_KEY"):
        return True
    try:
        import winreg  # type: ignore
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
            v, _ = winreg.QueryValueEx(k, "DEEPSEEK_API_KEY")
        if v:
            env["DEEPSEEK_API_KEY"] = v
            return True
    except Exception:
        pass
    return False


def emit(typ: str, **fields) -> None:
    payload = {"type": typ}
    payload.update(fields)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def main(argv: list[str]) -> int:
    codex = find_codex()
    if not codex:
        emit("error", message="codex_not_found")
        return 127

    env = os.environ.copy()
    env.setdefault("HOME", os.path.expanduser("~"))
    if not inject_credentials(env):
        emit("error", message="codex_credentials_unavailable")
        return 78

    timeout_s = float(os.environ.get("CODEX_TIMEOUT_S", "600"))
    try:
        completed = subprocess.run(
            [codex] + argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            env=env,
        )
    except subprocess.TimeoutExpired:
        emit("error", message="codex_timeout")
        return 124
    except Exception as e:
        emit("error", message=str(e)[:200])
        return 1

    out = completed.stdout.decode("utf-8", "replace")
    err = completed.stderr.decode("utf-8", "replace")

    # Codex 的 JSONL 原样透传
    if out:
        sys.stdout.write(out)
        if not out.endswith("\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()

    if completed.returncode != 0:
        emit("adapter.stderr_tail", text=err[-400:])
    return completed.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
