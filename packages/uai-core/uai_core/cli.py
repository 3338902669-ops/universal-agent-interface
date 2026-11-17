"""uai 命令行：不依赖任何宿主应用。

    uai doctor              环境自检（平台/依赖/UAI_HOME/锁/进程）
    uai detect [--json]     三层检测本机 agent（没装≠认证失败≠版本不兼容）
    uai seats --from-detect 把检测结果写成 seats.yml
    uai status              中枢/席位锁状态 + 进程计数（psutil，不调 shell）
    uai serve               监督循环（幂等：已有监督在跑就退出 0）
    uai install-service     注册用户级开机自启
    uai uninstall-service   移除自启
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config, paths
from .detect import detector
from .oslayer import proc, service
from .oslayer.lock import FileLock, SingletonError

EXIT_ALREADY_RUNNING = 3


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def cmd_doctor(args: argparse.Namespace) -> int:
    deps = {}
    for name in ("psutil", "yaml", "nats", "neo4j"):
        try:
            __import__(name)
            deps[name] = True
        except Exception:
            deps[name] = False
    home = paths.ensure_home()
    _print({
        "ok": True,
        "platform": {"os": os.name, "sys": sys.platform, "python": sys.version.split()[0]},
        "paths": {"uai_home": str(home), "base": str(paths.base_dir()),
                  "seats": str(paths.seats_file()), "ledger": str(paths.ledger_file())},
        "deps": deps,
        "autostart": service.status(),
        "nats_url": config.load().get("hub", {}).get("nats_url"),
        "python_executable": sys.executable,
    })
    return 0


def cmd_detect(args: argparse.Namespace) -> int:
    only = [s.strip() for s in (args.only or "").split(",") if s.strip()] or None
    report = detector.detect(args.probes, only=only, timeout_s=args.timeout)
    if args.json:
        _print(report)
    else:
        _print({"counts": report["counts"], "ready": report["ready"],
                "platform": report["platform"]})
        for row in report["results"]:
            print("%-10s %-20s %-22s %s" % (row["id"], row["state"],
                                             row.get("version") or "-",
                                             (row.get("binary") or "-")[:60]))
    return 0


def cmd_seats(args: argparse.Namespace) -> int:
    cfg = config.load(args.config)
    if args.from_detect:
        report = detector.detect(args.probes, timeout_s=args.timeout)
        cfg["seats"] = config.seats_from_detection(report)
        path = config.save(cfg, args.config)
        _print({"saved": str(path), "seats": len(cfg["seats"]),
                "enabled": [s["id"] for s in cfg["seats"] if s.get("enabled")]})
        return 0
    _print(cfg)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    hub = FileLock(paths.hub_lock(), namespace="uai-hub", ttl_s=args.ttl)
    hub_status = hub.status()
    seats = {}
    for seat in (config.load(args.config).get("seats") or []):
        sid = str(seat.get("id") or "")
        if not sid:
            continue
        lock = FileLock(paths.seat_lock(sid), namespace="uai-seat:" + sid, ttl_s=args.ttl)
        st = lock.status()
        seats[sid] = {"held": st.get("held"), "pid": (st.get("holder") or {}).get("pid"),
                      "why": st.get("why")}
    _print({
        "hub": hub_status,
        "seats": seats,
        "processes": {
            "hub": proc.count(contains=("hub.py",)),
            "interface": proc.count(contains=("interface.py",)),
            "supervisor": proc.count(contains=("uai_core", "serve")),
        },
    })
    return 0


def _spawn(cmd: list[str], log_path: Path) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("UAI_HOME", str(paths.uai_home()))
    env.setdefault("PYTHONUNBUFFERED", "1")
    handle = open(log_path, "a", encoding="utf-8")
    handle.write("\n=== spawn %s at %s ===\n" % (" ".join(cmd), time.strftime("%Y-%m-%dT%H:%M:%S")))
    handle.flush()
    return subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT,
                            cwd=str(paths.uai_home()), env=env)


def cmd_serve(args: argparse.Namespace) -> int:
    """监督循环：一次只有一个（supervisor 锁），子进程退出按退避重启。"""
    cfg = config.load(args.config)
    procs = cfg.get("processes") or []
    problems = config.validate(cfg)
    if problems:
        _print({"ok": False, "reason": "invalid_config", "problems": problems})
        return 2
    if not procs:
        _print({"ok": False, "reason": "no_processes_configured",
                "hint": "seats.yml 里加 processes: [{name, command: [...]}]"})
        return 2
    if args.dry_run:
        _print({"ok": True, "dry_run": True, "processes": procs})
        return 0

    guard = FileLock(paths.run_dir() / "supervisor.lock",
                     namespace="uai-supervisor", ttl_s=args.ttl)
    ok, info = guard.acquire()
    if not ok:
        _print({"ok": False, "reason": info.get("reason"), "holder": info.get("holder")})
        return EXIT_ALREADY_RUNNING

    paths.ensure_home()
    backoff = float(cfg.get("supervisor", {}).get("restart_backoff_s", 5.0))
    max_backoff = float(cfg.get("supervisor", {}).get("max_backoff_s", 60.0))
    children: dict[str, subprocess.Popen] = {}
    waits: dict[str, float] = {}
    stopping = {"flag": False}

    def _stop(signum, frame):
        stopping["flag"] = True

    for name in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, name), _stop)
        except Exception:
            pass

    _print({"ok": True, "event": "supervisor_started", "pid": os.getpid(),
            "processes": [p.get("name") for p in procs]})
    try:
        while not stopping["flag"]:
            now = time.time()
            for spec in procs:
                name = str(spec.get("name") or spec.get("command", ["?"])[0])
                child = children.get(name)
                if child is not None and child.poll() is None:
                    continue
                if child is not None and child.poll() is not None:
                    waits[name] = now + backoff
                    children.pop(name, None)
                if now < waits.get(name, 0):
                    continue
                cmd = [str(c) for c in spec.get("command") or []]
                if not cmd:
                    continue
                log = paths.logs_dir() / (name + ".log")
                try:
                    children[name] = _spawn(cmd, log)
                except Exception as exc:
                    _print({"event": "spawn_failed", "name": name, "error": str(exc)[:200]})
                    waits[name] = now + max_backoff
                backoff = min(max_backoff, backoff * 2)
            try:
                guard.heartbeat()
            except SingletonError:
                _print({"event": "lost_supervisor_lock"})
                break
            time.sleep(max(1.0, args.poll_s))
    finally:
        for name, child in children.items():
            if child.poll() is None:
                try:
                    child.terminate()
                except Exception:
                    pass
        for name, child in children.items():
            try:
                child.wait(timeout=10)
            except Exception:
                try:
                    child.kill()
                except Exception:
                    pass
        guard.release()
    return 0


def cmd_install_service(args: argparse.Namespace) -> int:
    _print(service.install())
    return 0


def cmd_uninstall_service(args: argparse.Namespace) -> int:
    _print(service.uninstall())
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="uai", description="UAI collaboration core")
    ap.add_argument("--config", default=None, help="seats.yml 路径")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    d = sub.add_parser("detect")
    d.add_argument("--json", action="store_true")
    d.add_argument("--only", default="")
    d.add_argument("--probes", default=None)
    d.add_argument("--timeout", type=float, default=8.0)
    s = sub.add_parser("seats")
    s.add_argument("--from-detect", action="store_true")
    s.add_argument("--probes", default=None)
    s.add_argument("--timeout", type=float, default=8.0)
    st = sub.add_parser("status")
    st.add_argument("--ttl", type=float, default=600.0)
    sv = sub.add_parser("serve")
    sv.add_argument("--ttl", type=float, default=600.0)
    sv.add_argument("--poll-s", type=float, default=3.0)
    sv.add_argument("--dry-run", action="store_true")
    sub.add_parser("install-service")
    sub.add_parser("uninstall-service")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    table = {
        "doctor": cmd_doctor,
        "detect": cmd_detect,
        "seats": cmd_seats,
        "status": cmd_status,
        "serve": cmd_serve,
        "install-service": cmd_install_service,
        "uninstall-service": cmd_uninstall_service,
    }
    return table[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
