"""常驻面健康检查（F-06）：一条命令回答"中台到底还活着吗、有没有被叠成两套"。

为什么需要它：2026-11-17 之前，常驻面唯一的"检查"是"看一眼进程在不在"。
结果两次叠加实例都没被发现，账本被写出重复记录、哈希链被写分叉（UAI-12/UAI-14）。
所以这里的判定不靠"进程存在"，而靠**机制事实**：

  1. NATS 4223 在听，JetStream 可用；
  2. 中枢单例锁被**恰好一个**活进程持有（锁是 OS 独占锁，重复启动会拿不到）；
  3. 每个席位的锁被**恰好一个**活进程持有（席位锁同理）；
  4. 通过 NATS 真问一次 `hub.reconcile`——进程在 ≠ 能干活；
  5. 图谱（Neo4j 7687）可达；
  6. 账本新鲜（最后一条记录的时间没有停在很远以前）；
  7. 心跳文件新鲜（`data/heartbeats.jsonl`）；
  8. 有且只有一个监督进程（`run-uai-hub.ps1`）——没有它，子进程死了没人拉起来。

退出码 0 表示全过；1 表示有失败项（打印每一项的 ok/detail）。
加 `--repair` 时若判定不健康，会调用启动入口（幂等：启动脚本先清场再起）后复检。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hub_singleton as HS          # noqa: E402  (uai_core 的薄壳)
from uai_core import paths           # noqa: E402
from uai_core.oslayer import proc as UPROC  # noqa: E402  跨平台进程枚举（psutil）

# 数据面：优先 UAI_BASE，其次 uai_core 默认（~/.uai/base）。
# 旧部署把 UAI_BASE 指到 comms-v2 那一份即可，路径不再写死在代码里。
HUB_BASE = Path(os.environ.get("UAI_BASE") or paths.base_dir())
NATS_URL = "nats://127.0.0.1:4223"
AUTOSTART_VBS = r"C:\Users\33389\.shared\uai-hub\start-uai-hub.vbs"
NATS_CONF = r"C:\Users\33389\.shared\uai-hub\nats-hub.conf"
LEDGER_FRESH_S = 24 * 3600          # 账本 24 小时没有新记录视为不新鲜（可按需调）
HEARTBEAT_FRESH_S = 120


def check(name, ok, detail=""):
    return {"name": name, "ok": bool(ok), "detail": detail}


def _pid_alive(pid: int) -> bool:
    return HS._pid_alive(int(pid or 0))


def _lock_probe(path: Path, namespace: str) -> dict:
    """只读探针：有没有人持锁、持有者是谁、是否还活着。"""
    if not path.exists():
        return {"exists": False, "held": False, "pid": None, "alive": False}
    probe = HS.HubSingleton(path, namespace=namespace, ttl_s=600)
    st = probe.status()
    pid = ((st.get("holder") or {}).get("pid"))
    return {"exists": True, "held": bool(st.get("held")), "pid": pid,
            "alive": _pid_alive(pid) if pid else False, "why": st.get("why"),
            "stale": st.get("stale")}


def check_nats_tcp() -> dict:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", 4223), timeout=2):
            return check("nats_4223_listening", True, "tcp ok")
    except Exception as exc:
        return check("nats_4223_listening", False, str(exc)[:120])


async def _nats_checks() -> list[dict]:
    import nats
    out = []
    try:
        nc = await nats.connect(NATS_URL, connect_timeout=3, max_reconnect_attempts=0)
    except Exception as exc:
        return [check("nats_connect", False, str(exc)[:120])]
    try:
        out.append(check("nats_connect", True, NATS_URL))
        try:
            js = nc.jetstream(timeout=3)
            info = await js.account_info()
            out.append(check("jetstream_available", True,
                             "streams=%s" % getattr(info, "streams", "?")))
        except Exception as exc:
            out.append(check("jetstream_available", False, str(exc)[:120]))

        try:
            msg = await nc.request("hub.reconcile", b"{}", timeout=8)
            resp = json.loads(msg.data.decode("utf-8"))
            out.append(check("hub_answers_reconcile", bool(resp.get("ok")),
                             "gap_count=%s" % resp.get("gap_count")))
        except Exception as exc:
            out.append(check("hub_answers_reconcile", False, str(exc)[:120]))

        try:
            msg = await nc.request("hub.debug",
                                   json.dumps({"action": "snapshot"}).encode("utf-8"),
                                   timeout=5)
            resp = json.loads(msg.data.decode("utf-8"))
            sessions = resp.get("interfaces") or {}
            active = [sid for sid, s in sessions.items() if s.get("active")]
            stale = [sid for sid, s in sessions.items() if s.get("stale")]
            out.append(check("seats_registered", len(active) >= 1,
                             "active=%s stale=%s" % (",".join(sorted(active)),
                                                     ",".join(sorted(stale)))))
        except Exception as exc:
            out.append(check("seats_registered", False, str(exc)[:120]))
    finally:
        try:
            await nc.close()
        except Exception:
            pass
    return out


def check_graph() -> dict:
    try:
        from neo4j import GraphDatabase
        drv = GraphDatabase.driver("bolt://127.0.0.1:7687",
                                   auth=("neo4j", "graphiti"), connection_timeout=2.0)
        drv.verify_connectivity()
        drv.close()
        return check("graph_7687_reachable", True, "bolt ok")
    except Exception as exc:
        return check("graph_7687_reachable", False, str(exc)[:120])


def check_ledger_fresh() -> dict:
    path = HUB_BASE / "data" / "ledger.jsonl"
    if not path.exists():
        return check("ledger_fresh", False, "missing: %s" % path)
    last_ts = None
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("ts"):
                last_ts = rec["ts"]
    if not last_ts:
        return check("ledger_fresh", False, "no ts")
    from datetime import datetime, timezone
    try:
        dt = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            dt = datetime.strptime(last_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            return check("ledger_fresh", True, "ts unparsed: %s" % last_ts)
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return check("ledger_fresh", age <= LEDGER_FRESH_S,
                 "last_ts=%s age=%.0fs" % (last_ts, age))


def check_heartbeats_fresh() -> dict:
    path = HUB_BASE / "data" / "heartbeats.jsonl"
    if not path.exists():
        return check("heartbeats_fresh", False, "missing: %s" % path)
    mtime_age = time.time() - path.stat().st_mtime
    return check("heartbeats_fresh", mtime_age <= HEARTBEAT_FRESH_S,
                 "mtime_age=%.0fs" % mtime_age)


def check_supervisor() -> dict:
    """监督进程在不在：没有它，子进程死了没人拉。

    两种形态都认：Python 监督（uai_core serve）与旧 PowerShell 监督（run-uai-hub.ps1）。
    用 psutil 直接读进程表，不 spawn 子壳——旧实现用 `-Command` 查进程时，
    检查脚本自己的命令行里带着匹配模式，于是把自己也数进去（曾误报 count=2）。
    """
    forms = (("uai_core", "serve"), ("run-uai-hub.ps1", "-File"))
    found = []
    for tokens in forms:
        found.extend(r["pid"] for r in UPROC.find(contains=tokens,
                                                 exclude_pids=(os.getpid(),)))
    pids = sorted(set(found))
    return check("supervisor_present", len(pids) == 1,
                 "count=%d pids=%s" % (len(pids), pids))

def seat_ids() -> list[str]:
    tok = HUB_BASE / ".lab-tokens"
    if not tok.exists():
        return []
    return sorted(p.stem for p in tok.glob("*.token"))


def collect() -> dict:
    checks: list[dict] = []
    checks.append(check_nats_tcp())

    hub_lock = HUB_BASE / "data" / "hub.lock"
    hp = _lock_probe(hub_lock, "uai-hub")
    checks.append(check("hub_singleton_held", hp["held"] and hp["alive"],
                        "pid=%s alive=%s why=%s" % (hp["pid"], hp["alive"], hp.get("why"))))

    for sid in seat_ids():
        lock = HUB_BASE / "data" / ("seat-%s.lock" % sid)
        sp = _lock_probe(lock, "uai-seat:%s" % sid)
        checks.append(check("seat_%s_singleton_held" % sid,
                            sp["held"] and sp["alive"],
                            "pid=%s alive=%s" % (sp["pid"], sp["alive"])))

    try:
        checks.extend(asyncio.run(_nats_checks()))
    except Exception as exc:
        checks.append(check("nats_checks", False, str(exc)[:120]))

    checks.append(check_graph())
    checks.append(check_ledger_fresh())
    checks.append(check_heartbeats_fresh())
    checks.append(check_supervisor())

    ok = all(c["ok"] for c in checks)
    return {"ok": ok, "checked_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "base": str(HUB_BASE), "checks": checks,
            "failed": [c["name"] for c in checks if not c["ok"]]}


def repair() -> int:
    print(json.dumps({"repair": "invoking autostart entry (idempotent)",
                      "vbs": AUTOSTART_VBS}, ensure_ascii=False))
    subprocess.run(["wscript.exe", AUTOSTART_VBS], timeout=60)
    for _ in range(24):
        time.sleep(5)
        res = collect()
        if res["ok"]:
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
    return 1



def _ps(script: str) -> str:
    res = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script],
                         capture_output=True, text=True, timeout=120)
    return res.stdout or ""


def _pids(kind: str) -> list[int]:
    if kind == "supervisor":
        script = ("Get-CimInstance Win32_Process -Filter \"Name='powershell.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*-File*run-uai-hub.ps1*' "
                  "-and $_.CommandLine -notlike '*-Command*' } | "
                  "Select-Object -ExpandProperty ProcessId")
    elif kind == "hub":
        script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*prototype*hub.py*' } | "
                  "Select-Object -ExpandProperty ProcessId")
    else:
        script = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
                  "Where-Object { $_.CommandLine -like '*interface.py*' } | "
                  "Select-Object -ExpandProperty ProcessId")
    return [int(x) for x in _ps(script).split() if x.strip().isdigit()]


def restart_plane() -> int:
    """显式重启常驻面（自启入口本身是幂等的，不会替你重启）。

    顺序：先杀监督 → 再杀中枢/席位 → 等清空 → 点自启入口 → 等健康。
    先杀监督是必须的：它会在 30s 内把子进程拉回来，先杀子进程等于白杀。
    """
    print(json.dumps({"restart": "begin", "step": "stop supervisor"}, ensure_ascii=False))
    for pid in _pids("supervisor"):
        _ps("Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid)
    time.sleep(1)
    for kind in ("hub", "seat"):
        for pid in _pids(kind):
            _ps("Stop-Process -Id %d -Force -ErrorAction SilentlyContinue" % pid)
    time.sleep(2)
    left = {"supervisor": _pids("supervisor"), "hub": _pids("hub"), "seat": _pids("seat")}
    print(json.dumps({"restart": "stopped", "left": left}, ensure_ascii=False))
    subprocess.run(["wscript.exe", AUTOSTART_VBS], timeout=60)
    for _ in range(30):
        time.sleep(5)
        res = collect()
        if res["ok"]:
            print(json.dumps({"restart": "healthy", **res}, ensure_ascii=False, indent=2))
            return 0
    print(json.dumps({"restart": "unhealthy", **collect()}, ensure_ascii=False, indent=2))
    return 1

def main(argv: list[str]) -> int:
    if "--restart" in argv:
        return restart_plane()
    res = collect()
    print(json.dumps(res, ensure_ascii=False, indent=2))
    if res["ok"]:
        return 0
    if "--repair" in argv:
        return repair()
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
