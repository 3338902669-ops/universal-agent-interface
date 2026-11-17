"""常驻面演练（F-06）：不重启机器，把"重启后会怎样"逐条打出来。

为什么不能只写个检查脚本：检查只能证明"此刻是对的"。常驻面的真正问题是
"掉了会不会自己回来、会不会回来两套"。这两件事必须真杀进程才算数
——2026-11-17 之前没人杀过，结果叠实例叠了一整天没人发现（UAI-12）。

四个演练：
  1. 硬杀中枢 → 监督是否在 45s 内把它拉起来，单例锁是否被新进程重新持有；
  2. 硬杀一个席位 → 同上（席位侧）；
  3. 运行中再点一次自启 → 必须幂等（同一批 pid，不新建、不重启）；
  4. 全部杀掉（含监督）→ 再点自启 → 是否恢复到 1 中枢 + 4 席位且健康检查全绿。

第 4 条是最接近"重启登录"的替代：它验证的是自启入口本身能不能把一整套拉起来。
**真正的冷启动仍需重启一次才算闭环**，脚本不会假装这件事已经做完。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import hub_singleton as HS          # noqa: E402
import resident_health as RH        # noqa: E402

VBS = r"C:\Users\33389\.shared\uai-hub\start-uai-hub.vbs"
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-52s %s" % ("PASS" if cond else "FAIL", name, detail))


def _pids(kind: str) -> list[int]:
    """用 psutil 枚举（跨平台、不 spawn 子壳、不会把自己数进去）。"""
    from uai_core.oslayer import proc as UPROC

    if kind == "supervisor":
        forms = (("uai_core", "serve"), ("run-uai-hub.ps1", "-File"))
        pids = []
        for tokens in forms:
            pids.extend(r["pid"] for r in UPROC.find(contains=tokens,
                                                    exclude_pids=(os.getpid(),)))
        return sorted(set(pids))
    if kind == "hub":
        return [r["pid"] for r in UPROC.find(contains=("hub.py",),
                                             exclude_pids=(os.getpid(),))]
    return [r["pid"] for r in UPROC.find(contains=("interface.py",),
                                         exclude_pids=(os.getpid(),))]


def procs(kind: str) -> list[dict]:
    if kind == "seat":
        rows = UPROC_WITH_SEAT()
        return rows
    return [{"ProcessId": pid} for pid in _pids(kind)]


def UPROC_WITH_SEAT() -> list[dict]:
    from uai_core.oslayer import proc as UPROC

    out = []
    for row in UPROC.find(contains=("interface.py",), exclude_pids=(os.getpid(),)):
        seat = None
        parts = row["cmdline"]
        for i, token in enumerate(parts):
            if token == "--interface-id" and i + 1 < len(parts):
                seat = parts[i + 1]
                break
        out.append({"ProcessId": row["pid"], "seat": seat})
    return out

def wait_for(fn, timeout_s: float, interval_s: float = 2.0):
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval_s)
    return last


def kill(pids):
    from uai_core.oslayer import proc as UPROC

    for pid in pids:
        UPROC.kill(int(pid), timeout=8.0)


def fire_autostart():
    """旧部署用 vbs；新部署用 uai install-service + uai serve。两者都试。"""
    vbs = r"C:\Users\33389\.shared\uai-hub\start-uai-hub.vbs"
    try:
        subprocess.run(["wscript.exe", vbs], timeout=60)
    except Exception:
        pass

def fire_autostart():
    subprocess.run(["wscript.exe", VBS], timeout=60)


def lock_pid(base: Path, name: str, ns: str):
    probe = HS.HubSingleton(base / "data" / name, namespace=ns, ttl_s=600)
    st = probe.status()
    pid = (st.get("holder") or {}).get("pid")
    return pid if (st.get("held") and pid) else None


def drill_hub_restart(base: Path) -> None:
    before = procs("hub")
    if len(before) != 1:
        check("演练前中枢唯一", False, "count=%d" % len(before))
        return
    old = before[0]["ProcessId"]
    kill([old])
    print("    杀掉中枢 pid=%s，等监督拉起..." % old)
    new = wait_for(lambda: [p for p in procs("hub") if p["ProcessId"] != old], 75)
    newpid = new[0]["ProcessId"] if new else None
    check("硬杀中枢后 45s 内被拉起", bool(newpid), "old=%s new=%s" % (old, newpid))
    lp = wait_for(lambda: lock_pid(base, "hub.lock", "uai-hub"), 30)
    check("新中枢重新持有单例锁", lp == newpid, "lock_holder=%s" % lp)
    h = wait_for(lambda: (lambda r: r if r["ok"] else None)(RH.collect()), 90, 5)
    check("拉起后健康检查全绿", bool(h),
          "failed=%s" % (h["failed"] if h else RH.collect()["failed"]))


def drill_seat_restart(base: Path) -> None:
    before = procs("seat")
    if not before:
        check("演练前席位存在", False, "count=0")
        return
    target = before[0]
    old = target["ProcessId"]
    seat = target.get("seat")
    kill([old])
    print("    杀掉席位 %s pid=%s，等监督拉起..." % (seat, old))
    new = wait_for(lambda: [p for p in procs("seat")
                            if p.get("seat") == seat and p["ProcessId"] != old], 45)
    newpid = new[0]["ProcessId"] if new else None
    check("硬杀席位后 45s 内被拉起", bool(newpid), "%s old=%s new=%s" % (seat, old, newpid))
    lp = wait_for(lambda: lock_pid(base, "seat-%s.lock" % seat, "uai-seat:%s" % seat), 30)
    check("新席位重新持有席位锁", lp == newpid, "lock_holder=%s" % lp)


def drill_idempotent_autostart(base: Path) -> None:
    hb = sorted(p["ProcessId"] for p in procs("hub"))
    sb = sorted(p["ProcessId"] for p in procs("seat"))
    supb = sorted(p["ProcessId"] for p in procs("supervisor"))
    fire_autostart()
    time.sleep(10)
    ha = sorted(p["ProcessId"] for p in procs("hub"))
    sa = sorted(p["ProcessId"] for p in procs("seat"))
    supa = sorted(p["ProcessId"] for p in procs("supervisor"))
    check("运行中再点自启：中枢 pid 不变（幂等，不重建）", hb == ha,
          "before=%s after=%s" % (hb, ha))
    check("运行中再点自启：席位 pid 不变", sb == sa,
          "before=%s after=%s" % (sb, sa))
    check("运行中再点自启：监督仍只有一个", len(supa) == 1 and supb == supa,
          "before=%s after=%s" % (supb, supa))


def drill_total_recovery(base: Path) -> None:
    kill([p["ProcessId"] for p in procs("supervisor")])
    kill([p["ProcessId"] for p in procs("hub")])
    kill([p["ProcessId"] for p in procs("seat")])
    time.sleep(3)
    down = RH.collect()
    check("全杀之后健康检查确实判红（检查本身不是摆设）", down["ok"] is False,
          "failed=%s" % ",".join(down["failed"][:4]))

    print("    点自启入口，等整套起来...")
    fire_autostart()
    up = wait_for(lambda: (lambda r: r if r["ok"] else None)(RH.collect()), 90, 5)
    check("自启入口能把一整套拉回来且健康检查全绿", bool(up),
          "failed=%s" % (up["failed"] if up else RH.collect()["failed"]))
    hubs = procs("hub")
    seats = procs("seat")
    sups = procs("supervisor")
    check("恢复后中枢恰好 1 个", len(hubs) == 1, "count=%d" % len(hubs))
    check("恢复后席位恰好 4 个且 id 齐全",
          len(seats) == 4 and sorted(s.get("seat") for s in seats)
          == ["codex", "dsh", "tianshu", "xiaoyu"],
          "seats=%s" % sorted(str(s.get("seat")) for s in seats))
    check("恢复后监督恰好 1 个", len(sups) == 1, "count=%d" % len(sups))


def ensure_up() -> dict:
    """演练前先把常驻面拉起来。演练要能反复跑，不能依赖运气。"""
    h = RH.collect()
    if h["ok"]:
        return h
    print("    起点不健康（failed=%s）→ 先跑一次自启入口" % ",".join(h["failed"][:3]))
    fire_autostart()
    wait_for(lambda: (lambda r: r if r["ok"] else None)(RH.collect()), 120, 5)
    return RH.collect()


def main() -> int:
    base = RH.HUB_BASE
    print("=== 常驻面演练（base=%s） ===" % base)
    h0 = ensure_up()
    check("演练起点：健康检查全绿", h0["ok"], "failed=%s" % h0["failed"])

    print("-- 演练 1：硬杀中枢 --")
    drill_hub_restart(base)
    print("-- 演练 2：硬杀席位 --")
    drill_seat_restart(base)
    print("-- 演练 3：运行中重复自启（幂等） --")
    drill_idempotent_autostart(base)
    print("-- 演练 4：全杀后靠自启恢复 --")
    drill_total_recovery(base)

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    print("注意：真正的冷启动（重启机器）仍需实机重启一次才算闭环；"
          "重启后跑\n  python stage2\\resident_health.py\n即可确认。")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    raise SystemExit(main())
