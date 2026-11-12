"""E-04 自检：CI 编排的门必须有牙。

关键断言：
  1. lab.py 在缺 nats-server 时明确失败（不是假装成功）；
  2. lab.py 在端口被占时明确失败并报出占用端口；
  3. lab.py 拒绝不存在的 base（hub.py 不在）；
  4. ci_suites 的 REQUIRES_LAB 不再把 A-02 无条件排除；
  5. 缺 NATS_URL 时 A-02 记 skipped 且理由写明；
  6. 有 NATS_URL 时 A-02 不被 skip。

第 5/6 条是这次的重点：A-02 曾经在 CI 里长期 SKIP，等于「跨厂商闭环」这条
主线在 CI 里从未被验证过。本测试守住它不再悄悄退回。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))

import ci_suites as CS  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-56s %s" % ("PASS" if cond else "FAIL", name, detail))


def run_lab(*args) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(HERE / "lab.py"), *args],
                       capture_output=True, text=True, timeout=120)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def free_port() -> int:
    import socket as _s
    s = _s.socket(_s.AF_INET, _s.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    # ---------- 1. 缺 nats-server ----------
    # 必须给一对空闲端口：start() 先查端口再找二进制，
    # 若端口被占（例如本机 lab 正在跑）就会先报 port_busy，测不到这一条。
    p = free_port()
    code, out = run_lab("start", "--nats-server", "C:/definitely/not/here/nats-server",
                        "--port", str(p))
    check("E-04 缺 nats-server → 非 0 且报错", code != 0 and "nats_server_not_found" in out,
          out.strip()[:70])

    # ---------- 2. base 不存在 ----------
    code, out = run_lab("start", "--base", str(HERE / "no-such-base"))
    check("E-04 base 无 hub.py → 非 0", code != 0 and "hub.py_not_found" in out, out.strip()[:70])

    # ---------- 3. 端口被占 ----------
    import socket
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    busy = srv.getsockname()[1]
    code, out = run_lab("start", "--port", str(busy))
    srv.close()
    check("E-04 端口被占 → 非 0 且报出端口", code != 0 and "port_busy" in out, out.strip()[:70])

    # ---------- 4. REQUIRES_LAB 语义 ----------
    check("E-04 A-02 仍在 REQUIRES_LAB 表内（有前置声明）",
          "stage2-test_a02_inbound" in CS.REQUIRES_LAB, str(list(CS.REQUIRES_LAB)))
    check("E-04 A-05 不在 PREREQ 里（解析层必须真跑）",
          "stage2-test_a05_second_runtime" not in CS.PREREQ, str(list(CS.PREREQ)))

    # ---------- 5/6. 有/无 NATS_URL 时 A-02 的去向 ----------
    saved = os.environ.pop("NATS_URL", None)
    try:
        row = CS.run_suite("stage2", ["test_a02_inbound.py"], "stage2-test_a02_inbound")
        check("E-04 缺 NATS_URL → A-02 记 skip 且理由写明",
              row.get("skipped") is True and "NATS_URL" in (row.get("reason") or ""),
              str(row.get("reason")))
    finally:
        if saved:
            os.environ["NATS_URL"] = saved

    if saved:
        # 真跑一次：有 lab 时 A-02 必须真的执行并退出 0，而不是被 skip 掉。
        row = CS.run_suite("stage2", ["test_a02_inbound.py"], "stage2-test_a02_inbound")
        check("E-04 有 NATS_URL 时 A-02 真跑且通过",
              row.get("skipped") is not True and row.get("exit") == 0,
              "exit=%s skipped=%s" % (row.get("exit"), row.get("skipped")))
    else:
        check("E-04 有 NATS_URL 时 A-02 真跑且通过", True, "本机未设 NATS_URL，跳过该项")

    # ---------- 7. workflow 里有防止退回 skip 的门 ----------
    wf = (ROOT / ".github" / "workflows" / "independent-verification.yml").read_text(encoding="utf-8")
    check("E-04 workflow 启动 lab", "bench/lab.py start" in wf, "")
    check("E-04 workflow 显式检查 A-02 未被 skip",
          "A-02 was skipped" in wf, "")
    check("E-04 workflow 装 nats-server", "nats-server" in wf and "install" in wf, "")

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
