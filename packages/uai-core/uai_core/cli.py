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
    # 中枢锁路径同样可能被 seats.yml 的 hub 槽位覆盖（团队 base 的 data/hub.lock）；
    # 只读 UAI_HOME 的默认路径会得到「未运行」，与实际不符。
    hub_lock_path = paths.hub_lock()
    for spec in (config.load(args.config).get("processes") or []):
        if str(spec.get("lock") or "") == "hub" and spec.get("lock_path"):
            hub_lock_path = Path(str(spec["lock_path"]))
            break
    hub = FileLock(hub_lock_path, namespace="uai-hub", ttl_s=args.ttl)
    hub_status = hub.status()
    # 席位的真实来源是监督配置里的槽位（seats.yml 的 processes[] 中 lock: seat:<id>），
    # 而不是 seats: []（那是 uai seats --from-detect 写的检测矩阵，默认是空的）。
    # 以前这里只读后者，导致 status 永远报 0 席、面板是空的。
    seats = {}
    cfg = config.load(args.config)
    for spec in (cfg.get("processes") or []):
        lock_spec = str(spec.get("lock") or "")
        if not lock_spec.startswith("seat:"):
            continue
        sid = lock_spec.split(":", 1)[1]
        lock_path = spec.get("lock_path")
        lock = FileLock(Path(lock_path) if lock_path else paths.seat_lock(sid),
                        namespace="uai-seat:" + sid, ttl_s=args.ttl)
        st = lock.status()
        seats[sid] = {"held": st.get("held"), "pid": (st.get("holder") or {}).get("pid"),
                      "why": st.get("why")}
    for seat in (cfg.get("seats") or []):
        sid = str(seat.get("id") or "")
        if sid and sid not in seats:
            lock = FileLock(paths.seat_lock(sid), namespace="uai-seat:" + sid, ttl_s=args.ttl)
            st = lock.status()
            seats[sid] = {"held": st.get("held"), "pid": (st.get("holder") or {}).get("pid"),
                          "why": st.get("why")}
    _print({
        "hub": hub_status,
        "seats": seats,
        # name_prefix 必须带上：宿主应用/调试脚本的命令行里可能带着这些字样
        # （例如 node -e 里写了 uai_core serve），只按子串数会把它们算成进程。
        "processes": {
            "hub": proc.count(contains=("hub.py",), name_prefix=("python",)),
            "interface": proc.count(contains=("interface.py",), name_prefix=("python",)),
            "supervisor": proc.count(contains=("uai_core", "serve"), name_prefix=("python",)),
        },
    })
    return 0




def cmd_guard(args: argparse.Namespace) -> int:
    """二级看护：监督者自己死了怎么办。

    为什么需要它：`uai serve` 由启动文件夹在登录时拉起，但如果它中途被杀
    （实测：DSH 桌面版重启会把整棵 wscript→pythonw 进程树一起带走），没有任何东西
    会把它拉回来。guard 被计划任务每 2 分钟叫一次，只做一件事：
    监督锁没人持有 → 拉起；有人持有 → 什么都不做。
    """
    from .oslayer.lock import FileLock

    probe = FileLock(paths.run_dir() / "supervisor.lock", namespace="uai-supervisor", ttl_s=600)
    st = probe.status()
    pid = (st.get("holder") or {}).get("pid")
    if st.get("held") and pid and proc.alive(int(pid)):
        _print({"ok": True, "event": "guard_noop", "holder_pid": int(pid)})
        return 0

    cfg = config.load(args.config)
    pythonw = _pythonw_path()
    cmd = [pythonw, "-m", "uai_core", "serve"]
    env = dict(os.environ)
    env.setdefault("UAI_HOME", str(paths.uai_home()))
    flags = 0
    if service.platform_kind() == "windows":
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    log_path = paths.logs_dir() / "guard.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8")
    handle.write("\n=== guard spawn %s at %s ===\n" % (" ".join(cmd), time.strftime("%Y-%m-%dT%H:%M:%S")))
    handle.flush()
    child = subprocess.Popen(cmd, stdout=handle, stderr=subprocess.STDOUT,
                             cwd=str(paths.uai_home()), env=env, creationflags=flags)
    time.sleep(float(cfg.get("guard", {}).get("verify_wait_s", 6.0)))
    st2 = probe.status()
    pid2 = (st2.get("holder") or {}).get("pid")
    ok = bool(st2.get("held") and pid2 and proc.alive(int(pid2)))
    _print({"ok": ok, "event": "guard_started" if ok else "guard_failed",
            "spawned_pid": child.pid, "holder_pid": pid2,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
    return 0 if ok else 1




GUARD_TASK = "UAI Guard"


def _guard_command() -> str:
    """计划任务要跑的命令行：pythonw -m uai_core guard。

    注意：**不要**往这里塞 --config —— 它是顶层参数，写在子命令后面会被 argparse 判成
    unrecognized argument 直接 exit 2，计划任务就变成「跑了个寂寞」（日志里什么都没有）。
    配置走默认解析（$UAI_HOME/seats.yml），需要改就用环境变量 UAI_HOME。
    """
    return f'"{_pythonw_path()}" -m uai_core guard'


def cmd_install_guard(args: argparse.Namespace) -> int:
    """注册二级看护：每 N 分钟检查一次监督者。"""
    if service.platform_kind() != "windows":
        _print({"ok": False, "reason": "unsupported_platform",
                "hint": "Linux/macOS 请用 systemd timer 或 launchd StartInterval 调 `uai guard`"})
        return 2
    cmdline = _guard_command()
    minutes = max(1, int(args.interval_minutes))
    res = subprocess.run([
        "schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes),
        "/TN", GUARD_TASK, "/TR", cmdline,
    ], capture_output=True, text=True)
    ok = res.returncode == 0
    _print({"ok": ok, "event": "guard_installed" if ok else "guard_install_failed",
            "task": GUARD_TASK, "interval_minutes": minutes, "command": cmdline,
            "detail": (res.stdout or res.stderr or "").strip()[:300]})
    return 0 if ok else 1


def cmd_uninstall_guard(args: argparse.Namespace) -> int:
    """移除二级看护。"""
    if service.platform_kind() != "windows":
        _print({"ok": False, "reason": "unsupported_platform"})
        return 2
    res = subprocess.run(["schtasks", "/Delete", "/F", "/TN", GUARD_TASK],
                         capture_output=True, text=True)
    ok = res.returncode == 0
    _print({"ok": ok, "event": "guard_removed" if ok else "guard_remove_failed",
            "detail": (res.stdout or res.stderr or "").strip()[:300]})
    return 0 if ok else 1

def _pythonw_path() -> str:
    """优先 pythonw.exe：guard/serve 是常驻进程，不该带控制台窗口。"""
    exe = Path(sys.executable)
    if service.platform_kind() == "windows":
        cand = exe.with_name("pythonw.exe")
        if cand.exists():
            return str(cand)
    return str(exe)

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




def _slot_owner(spec: dict) -> int | None:
    """该槽位的单例锁被哪个活进程持着？返回 pid 或 None。

    为什么要在 spawn 之前查：不查的话，监督器会先把子进程拉起来，子进程发现锁被占、
    立刻 exit 3，监督器再记一笔 slot_already_owned —— 进程表上就出现一两秒的幽灵进程，
    演练/巡检会把它们当成「pid 变了」。先查锁就没有这个抖动。
    """
    from .oslayer.lock import FileLock

    lock_path = spec.get("lock_path")
    ns = "default"
    if not lock_path:
        kind = str(spec.get("lock") or "")
        if kind == "hub":
            lock_path = str(paths.hub_lock())
            ns = "uai-hub"
        elif kind.startswith("seat:"):
            sid = kind.split(":", 1)[1]
            lock_path = str(paths.seat_lock(sid))
            ns = "uai-seat:" + sid
        else:
            return None
    try:
        st = FileLock(lock_path, namespace=ns, ttl_s=600).status()
    except Exception:
        return None
    pid = (st.get("holder") or {}).get("pid")
    if st.get("held") and pid and proc.alive(int(pid)):
        return int(pid)
    return None


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
    base_backoff = float(cfg.get("supervisor", {}).get("restart_backoff_s", 5.0))
    max_backoff = float(cfg.get("supervisor", {}).get("max_backoff_s", 60.0))
    slot_backoff: dict[str, float] = {}
    children: dict[str, subprocess.Popen] = {}
    waits: dict[str, float] = {}
    _external_logged: set[str] = set()
    stopping = {"flag": False}

    def _stop(signum, frame):
        stopping["flag"] = True
        _print({"event": "supervisor_signal", "signum": signum,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})

    for name in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, name), _stop)
        except Exception:
            pass

    exit_reason = "loop_end"
    _print({"ok": True, "event": "supervisor_started", "pid": os.getpid(),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
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
                    rc = child.returncode
                    children.pop(name, None)
                    if rc == EXIT_ALREADY_RUNNING:
                        # 该槽位已有别的实例在跑（单例锁拒绝）：接管不了就别反复重试，
                        # 否则会每 5s 起一个立刻退出的进程，把日志刷满。
                        if name not in _external_logged:
                            _external_logged.add(name)
                            _print({"event": "slot_already_owned", "name": name,
                            "exit_code": rc, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
                        continue
                    # 每个槽位独立退避：一次成功启动不能让别的槽位继续等
                    slot_backoff[name] = min(max_backoff, slot_backoff.get(name, base_backoff) * 2)
                    waits[name] = now + slot_backoff.get(name, base_backoff)
                if now < waits.get(name, 0):
                    continue
                cmd = [str(c) for c in spec.get("command") or []]
                if not cmd:
                    continue
                owner = _slot_owner(spec)
                if owner is not None:
                    if name not in _external_logged:
                        _external_logged.add(name)
                        _print({"event": "slot_adopted", "name": name, "holder_pid": owner,
                                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
                    continue
                port = spec.get("port")
                if port and proc.tcp_listening(int(port)):
                    # 端口已被外部实例占用：不起第二份（否则会绑定失败、无限重启）
                    if name not in _external_logged:
                        _external_logged.add(name)
                        _print({"event": "external_service_detected", "name": name,
                                "port": int(port)})
                    continue
                log = paths.logs_dir() / (name + ".log")
                try:
                    child = _spawn(cmd, log)
                    children[name] = child
                    _print({"event": "spawned", "name": name, "child_pid": child.pid,
                            "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
                except Exception as exc:
                    _print({"event": "spawn_failed", "name": name, "error": str(exc)[:200]})
                    waits[name] = now + max_backoff
                else:
                    # 启动成功 → 该槽位的退避复位（下一次异常从 base 重新开始）
                    slot_backoff[name] = base_backoff
            try:
                guard.heartbeat()
            except SingletonError as exc:
                exit_reason = "lost_supervisor_lock:%s" % exc
                _print({"event": "lost_supervisor_lock", "error": str(exc)[:120],
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
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
        _print({"event": "supervisor_exit", "pid": os.getpid(), "reason": exit_reason,
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S")})
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
    sub.add_parser("guard")
    g = sub.add_parser("install-guard")
    g.add_argument("--interval-minutes", type=int, default=2)
    sub.add_parser("uninstall-guard")
    return ap


def _ensure_streams() -> None:
    """pythonw / 服务方式启动时 stdout 是 None，print 会直接炸。这里兜到日志文件。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        paths.ensure_home()
        log_dir = paths.logs_dir()
        if sys.stdout is None:
            sys.stdout = open(log_dir / "supervisor.out.log", "a", encoding="utf-8", buffering=1)
        if sys.stderr is None:
            sys.stderr = open(log_dir / "supervisor.err.log", "a", encoding="utf-8", buffering=1)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    _ensure_streams()
    args = build_parser().parse_args(argv)
    table = {
        "doctor": cmd_doctor,
        "detect": cmd_detect,
        "seats": cmd_seats,
        "status": cmd_status,
        "serve": cmd_serve,
        "install-service": cmd_install_service,
        "uninstall-service": cmd_uninstall_service,
        "guard": cmd_guard,
        "install-guard": cmd_install_guard,
        "uninstall-guard": cmd_uninstall_guard,
    }
    return table[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
