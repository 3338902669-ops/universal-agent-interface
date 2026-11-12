"""跨平台实验台（lab）启停：nats-server + hub。

为什么不用 prototype/start_lab.ps1：那份是 PowerShell，只能在 Windows 跑，
而 CI 在 ubuntu 上——于是 A-02 端到端套件在 CI 里长期只能 SKIP。
本脚本只用标准库与 subprocess，两个平台同一套逻辑，让端到端验收真的跑起来。

用法：
    python bench/lab.py start --nats-server <path> [--base prototype] [--port 43321]
    python bench/lab.py stop
    python bench/lab.py status

约定：
  * 独立端口 43321/43322，避开生产 4222/8222；
  * 启动前检查端口占用，被占则明确报错而不是抢端口；
  * 就绪判据是真实信号（nats /varz 有 server_id、hub 输出 hub_ready），不是 sleep 猜；
  * pid 落盘 bench/.lab-pids.json，stop 时只杀自己启动的那两个 pid。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PID_FILE = ROOT / "bench" / ".lab-pids.json"


def port_busy(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def wait_http(url: str, timeout: float, check=None) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                body = r.read().decode("utf-8", "replace")
            if check is None or check(body):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def wait_file_contains(path: Path, needle: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if path.exists() and needle in path.read_text(encoding="utf-8", errors="replace"):
                return True
        except Exception:
            pass
        time.sleep(0.25)
    return False


def find_nats_server(explicit: str | None) -> str | None:
    # 显式指定的路径不存在 → 硬失败，不静默回退。
    # 否则「我明明指了 A，实际跑的是 B」这种偏差不会有人发现。
    if explicit:
        return str(explicit) if Path(explicit).exists() else None
    for cand in (os.environ.get("NATS_SERVER"), shutil.which("nats-server")):
        if cand and Path(cand).exists():
            return str(cand)
    for cand in (
        Path(r"C:\Users\33389\.shared\comms-lab\nats-server.exe"),
        ROOT / "nats-server",
        ROOT / "bin" / "nats-server",
    ):
        if Path(cand).exists():
            return str(cand)
    return None


def start(args) -> int:
    base = Path(args.base).resolve()
    if not (base / "hub.py").exists():
        print(json.dumps({"ok": False, "error": "hub.py_not_found", "base": str(base)}))
        return 2

    for port in (args.port, args.port + 1):
        if port_busy(port):
            print(json.dumps({"ok": False, "error": "port_busy", "port": port}))
            return 3

    nats_exe = find_nats_server(args.nats_server)
    if not nats_exe:
        print(json.dumps({"ok": False, "error": "nats_server_not_found"}))
        return 4

    logs = base / "logs"
    logs.mkdir(exist_ok=True)
    nats_out = (logs / "ci-nats-stdout.log").open("w", encoding="utf-8")
    nats_err = (logs / "ci-nats-stderr.log").open("w", encoding="utf-8")

    # 与 prototype/nats-test.conf 同参，但端口可指定，便于 CI 复用同一脚本。
    conf = base / "logs" / "ci-nats.conf"
    conf.write_text(
        "server_name: uai-ci-lab\n"
        "listen: 127.0.0.1:%d\n"
        "http_port: %d\n"
        "max_connections: 64\n"
        "max_payload: 1048576\n" % (args.port, args.port + 1),
        encoding="utf-8",
    )

    nats = subprocess.Popen([nats_exe, "-c", str(conf)], cwd=str(base),
                            stdout=nats_out, stderr=nats_err)

    if not wait_http("http://127.0.0.1:%d/varz" % (args.port + 1), 15,
                     check=lambda b: "server_id" in b):
        nats.kill()
        tail = (logs / "ci-nats-stderr.log").read_text(encoding="utf-8", errors="replace")[-400:]
        print(json.dumps({"ok": False, "error": "nats_not_ready", "stderr_tail": tail}))
        return 5

    hub_out_path = logs / "ci-hub-stdout.log"
    hub_out = hub_out_path.open("w", encoding="utf-8")
    hub_err = (logs / "ci-hub-stderr.log").open("w", encoding="utf-8")

    hub = subprocess.Popen(
        [sys.executable, str(base / "hub.py"), "--base", str(base),
         "--nats-url", "nats://127.0.0.1:%d" % args.port,
         "--lease-seconds", str(args.lease_seconds)],
        cwd=str(base), stdout=hub_out, stderr=hub_err,
    )

    if not wait_file_contains(hub_out_path, "hub_ready", 25):
        tail = (logs / "ci-hub-stderr.log").read_text(encoding="utf-8", errors="replace")[-400:]
        hub.kill()
        nats.kill()
        print(json.dumps({"ok": False, "error": "hub_not_ready", "stderr_tail": tail}))
        return 6

    PID_FILE.write_text(json.dumps({"nats_pid": nats.pid, "hub_pid": hub.pid,
                                    "nats_url": "nats://127.0.0.1:%d" % args.port,
                                    "base": str(base)}), encoding="utf-8")
    print(json.dumps({"ok": True, "nats_pid": nats.pid, "hub_pid": hub.pid,
                      "nats_url": "nats://127.0.0.1:%d" % args.port,
                      "monitor": "http://127.0.0.1:%d/varz" % (args.port + 1)}))
    return 0


def stop(_args) -> int:
    if not PID_FILE.exists():
        print(json.dumps({"ok": True, "note": "no_pid_file"}))
        return 0
    pids = json.loads(PID_FILE.read_text(encoding="utf-8"))
    killed = []
    for key in ("hub_pid", "nats_pid"):
        pid = pids.get(key)
        if not pid:
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True)
            else:
                os.kill(pid, 15)
            killed.append(pid)
        except Exception:
            pass
    PID_FILE.unlink(missing_ok=True)
    print(json.dumps({"ok": True, "stopped": killed}))
    return 0


def status(_args) -> int:
    if not PID_FILE.exists():
        print(json.dumps({"running": False}))
        return 1
    pids = json.loads(PID_FILE.read_text(encoding="utf-8"))
    print(json.dumps({"running": True, **pids}))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("start")
    s.add_argument("--nats-server", default=None)
    s.add_argument("--base", default=str(ROOT / "prototype"))
    s.add_argument("--port", type=int, default=43321)
    s.add_argument("--lease-seconds", type=int, default=300)
    s.set_defaults(func=start)

    sub.add_parser("stop").set_defaults(func=stop)
    sub.add_parser("status").set_defaults(func=status)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
