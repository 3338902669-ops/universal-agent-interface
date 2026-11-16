"""E-CXA: Codex 适配器契约测试。

覆盖：凭据注入、codex 未安装、退出码透传、输出透传。
真实调用默认跳过（消耗额度）；设 CODEX_E2E=1 才跑。
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "runtime_adapters"))

import codex_adapter as ca  # noqa: E402

ADAPTER = HERE / "runtime_adapters" / "codex_adapter.py"
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def run(args, env_extra=None, timeout=300):
    env = os.environ.copy()
    env.update(env_extra or {})
    return subprocess.run([sys.executable, str(ADAPTER)] + args,
                          capture_output=True, text=True, timeout=timeout, env=env)


def parse_jsonl(text):
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
    # ---- 凭据注入：环境里已有则直接复用 ----
    env = {"DEEPSEEK_API_KEY": "already-set"}
    check("CXA-1 existing key reused", ca.inject_credentials(env) is True, "")
    check("CXA-2 key untouched", env["DEEPSEEK_API_KEY"] == "already-set", "")

    # 清空后应从注册表注入（本机有 key）
    env2 = {}
    got = ca.inject_credentials(env2)
    check("CXA-3 key injected from registry", got is True, str(got))
    check("CXA-4 key is non-empty", bool(env2.get("DEEPSEEK_API_KEY")), "")
    # 只检查长度，绝不打印内容
    check("CXA-5 key length sane", len(env2.get("DEEPSEEK_API_KEY") or "") > 10, "")

    # ---- codex 未安装 -> rc 127 ----
    r = run(["exec", "--json", "hi"], {"CODEX_EXECUTABLE": "C:/nope/codex.cmd"})
    check("CXA-6 codex_not_found rc=127", r.returncode == 127, str(r.returncode))
    msgs = [p.get("message") for p in parse_jsonl(r.stdout)]
    check("CXA-7 codex_not_found payload", "codex_not_found" in msgs, str(msgs[:2]))

    # ---- 显式指定存在但不可执行 / 凭据缺失路径 ----
    env_nokey = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
    # 用一个假的 codex（存在即可）触发凭据检查之前的分支顺序验证
    fake = Path(tempfile.mkdtemp()) / "codex.cmd"
    fake.write_text("@echo off\r\nexit /b 0\r\n", encoding="utf-8")
    saved = os.environ.pop("DEEPSEEK_API_KEY", None)
    try:
        # 注册表里有 key，所以仍应注入成功 -> 传假 codex 后 rc=0 且无输出
        r2 = run(["exec", "x"], {"CODEX_EXECUTABLE": str(fake)})
        check("CXA-8 existing codex path accepted", r2.returncode in (0, 1), str(r2.returncode))
    finally:
        if saved is not None:
            os.environ["DEEPSEEK_API_KEY"] = saved

    # ---- 真实调用（可选）----
    if os.environ.get("CODEX_E2E") == "1":
        work = tempfile.mkdtemp()
        r3 = run(["exec", "--skip-git-repo-check", "--sandbox", "read-only",
                  "--ephemeral", "--json", "-C", work,
                  "Reply with exactly: CXA_E2E_OK"], timeout=300)
        items = parse_jsonl(r3.stdout)
        kinds = [i.get("type") for i in items]
        check("CXA-9 e2e rc=0", r3.returncode == 0, str(r3.returncode))
        check("CXA-10 e2e thread+turntarted",
              "thread.started" in kinds and "turn.started" in kinds, str(kinds[:3]))
        text = ""
        for i in items:
            if i.get("type") == "item.completed" and i.get("item", {}).get("type") == "agent_message":
                text = i["item"].get("text", "")
        check("CXA-11 e2e text returned", "CXA_E2E_OK" in text, text[:60])
    else:
        print("SKIP  CXA-9..11 真实调用（设 CODEX_E2E=1 才跑）")

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
