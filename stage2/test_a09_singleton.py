"""A-09 自检：中枢单例守卫（OS 独占锁版）。

十三项行为：
  1. 首个实例拿到写权；
  2. 第二个实例被拒，且能报出持有者；**拿不到写权不得派发**；
  3. 持有者心跳续期，非持有者心跳被拒；
  4. 复用同一实例幂等；
  5. 持有者被强杀 → 允许接管，且 takeover 事件**落盘**（真的杀子进程，不是伪造 pid）；
  6. 活进程持锁但 TTL 过期 → **不许夺权**，只报 wedged（旧版会夺，等于双写）；
  7. 同一文件上的遗留 holder 换了 namespace → 接管事件显式记 namespace_changed；
  8. 释放后不再持有；
  9. **多进程真并发**：8 个进程同时抢同一把锁，恰好 1 个成功。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import hub_singleton as HS

PASSES, FAILS = [], []

HERE = os.path.dirname(os.path.abspath(__file__))

CHILD = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import hub_singleton as HS
sentinel = sys.argv[3]
deadline = time.time() + 15
while not os.path.exists(sentinel) and time.time() < deadline:
    time.sleep(0.005)
g = HS.HubSingleton(sys.argv[2], namespace="lab", ttl_s=30)
ok, info = g.acquire()
print(json.dumps({"pid": os.getpid(), "ok": ok, "reason": info.get("reason")}), flush=True)
if ok:
    time.sleep(0.5)
    g.release()
'''

CHILD_HOLD = r'''
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
import hub_singleton as HS
g = HS.HubSingleton(sys.argv[2], namespace="lab", ttl_s=30)
ok, info = g.acquire()
print(json.dumps({"pid": os.getpid(), "ok": ok}), flush=True)
time.sleep(120)
'''



def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def scenario_basic(tmp):
    path = os.path.join(tmp, "basic.lock")
    a = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okA, infoA = a.acquire()
    check("A-09 首个实例取得写权", okA is True, str(infoA.get("reason")))

    okA2, infoA2 = a.acquire()
    check("A-09 同实例重复 acquire 幂等", okA2 is True and infoA2.get("reason") == "already_held",
          str(infoA2.get("reason")))
    check("A-09 OS 锁文件已建立", os.path.exists(str(path) + ".oslock"), "")
    check("A-09 held=True", a.held is True, "")

    b = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okB, infoB = b.acquire()
    check("A-09 第二个实例被拒", okB is False, str(infoB.get("why")))
    check("A-09 拒绝时能报出持有者 pid",
          infoB.get("holder", {}).get("pid") == os.getpid(),
          str(infoB.get("holder", {}).get("pid")))

    try:
        b.assert_can_dispatch()
    except HS.SingletonError as e:
        check("A-09 无写权时拒绝派发（不降级继续派）", "refusing to dispatch" in str(e), str(e)[:44])
    else:
        check("A-09 无写权时拒绝派发（不降级继续派）", False, "竟然允许派发")

    hb = a.heartbeat()
    check("A-09 持有者心跳成功", hb.get("ok") is True, "")
    try:
        b.heartbeat()
    except HS.SingletonError as e:
        check("A-09 非持有者心跳被拒", "not_holding" in str(e), str(e)[:30])
    else:
        check("A-09 非持有者心跳被拒", False, "未抛错")

    st = HS.HubSingleton(path, namespace="lab", ttl_s=30).status()
    check("A-09 有人持锁时 status 报持有", st.get("held") is True, str(st.get("why")))

    a.release()
    st = HS.HubSingleton(path, namespace="lab", ttl_s=30).status()
    check("A-09 释放后不再持有（元数据已清）",
          st.get("held") is False and st.get("holder") is None, str(st))


def scenario_takeover(tmp):
    """真死：子进程持锁 → 强杀 → 父进程接管（不是伪造 pid）。"""
    path = os.path.join(tmp, "takeover.lock")
    script = os.path.join(tmp, "holder.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(CHILD_HOLD)

    child = subprocess.Popen([sys.executable, script, HERE, path],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    first = child.stdout.readline()
    try:
        holder = json.loads(first)
    except Exception:
        holder = {}
    check("A-09 子进程先持锁", holder.get("ok") is True, first.strip()[:60])
    child_pid = holder.get("pid")

    b = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okB, infoB = b.acquire()
    check("A-09 活着的子进程持锁时父进程被拒", okB is False
          and (infoB.get("holder") or {}).get("pid") == child_pid,
          "holder_pid=%s" % (infoB.get("holder") or {}).get("pid"))

    child.kill()
    child.wait(timeout=30)
    deadline = time.time() + 10
    while time.time() < deadline:
        if HS.HubSingleton(path, namespace="lab", ttl_s=30).status().get("held") is False:
            break
        time.sleep(0.05)

    c = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okC, infoC = c.acquire()
    check("A-09 持有者被强杀 → 允许接管", okC is True and infoC.get("reason") == "takeover",
          str(infoC.get("reason")))
    check("A-09 接管后 generation 递增",
          c.holder is not None and c.holder.generation == 2,
          str(c.holder.generation if c.holder else None))

    on_disk = json.loads(open(path, encoding="utf-8").read())
    hist = on_disk.get("history") or []
    check("A-09 接管事件落盘（持有者死了也查得到）",
          len(hist) == 1 and hist[0].get("event") == "takeover"
          and hist[0].get("reason") == "os_lock_released",
          json.dumps(hist[0].get("previous", {}))[:60] if hist else "no history")
    check("A-09 接管事件记录前持有者已死",
          bool(hist) and hist[0].get("previous_pid_alive") is False,
          str(hist[0].get("previous_pid_alive")) if hist else "")
    check("A-09 接管事件记录前持有者 pid",
          bool(hist) and (hist[0].get("previous") or {}).get("pid") == child_pid,
          str((hist[0].get("previous") or {}).get("pid")) if hist else "")

    c.release()

def scenario_ttl_wedged(tmp):
    path = os.path.join(tmp, "ttl.lock")
    a = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    a.acquire()
    rec = json.loads(open(path, encoding="utf-8").read())
    rec["heartbeat_at"] = time.time() - 3600
    open(path, "w", encoding="utf-8").write(json.dumps(rec))

    b = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okB, infoB = b.acquire()
    check("A-09 TTL 过期但活进程持锁 → 不夺权（旧版会双写）", okB is False,
          str(infoB.get("why")))
    st = b.status()
    check("A-09 TTL 过期只报 wedged",
          st.get("stale") is True and st.get("why") == "ttl_expired_wedged",
          str(st.get("why")))
    a.release()


def scenario_namespace(tmp):
    path = os.path.join(tmp, "ns.lock")
    a = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    a.acquire()
    a.abandon()
    e = HS.HubSingleton(path, namespace="other", ttl_s=30)
    okE, infoE = e.acquire()
    check("A-09 死持有者的锁可跨 namespace 接管", okE is True, str(infoE.get("reason")))
    ev = e.takeover_events[0] if e.takeover_events else {}
    check("A-09 跨 namespace 接管显式记 namespace_changed",
          ev.get("namespace_changed") is True and ev.get("from_namespace") == "lab",
          str(ev.get("from_namespace")))
    e.release()


def scenario_concurrent(tmp):
    path = os.path.join(tmp, "race.lock")
    sentinel = os.path.join(tmp, "go")
    script = os.path.join(tmp, "child.py")
    with open(script, "w", encoding="utf-8") as f:
        f.write(CHILD)

    procs = [subprocess.Popen([sys.executable, script, HERE, path, sentinel],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(8)]
    time.sleep(0.4)
    open(sentinel, "w", encoding="utf-8").write("go")

    results = []
    for p in procs:
        out, err = p.communicate(timeout=60)
        line = (out or "").strip().splitlines()
        if line:
            try:
                results.append(json.loads(line[-1]))
            except Exception:
                results.append({"ok": None, "raw": out, "err": err})
        else:
            results.append({"ok": None, "raw": out, "err": err})

    winners = [r for r in results if r.get("ok") is True]
    losers = [r for r in results if r.get("ok") is False]
    check("A-09 8 进程并发抢占：恰好 1 个成功", len(winners) == 1,
          "winners=%d losers=%d" % (len(winners), len(losers)))
    check("A-09 并发失败方全部报 held_by_other",
          all(r.get("reason") == "held_by_other" for r in losers),
          json.dumps(sorted({r.get("reason") for r in losers}))[:60])
    check("A-09 并发后无人持锁（全部释放）",
          HS.HubSingleton(path, namespace="lab", ttl_s=30).status().get("held") is False, "")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="uai-singleton-")
    scenario_basic(tmp)
    scenario_takeover(tmp)
    scenario_ttl_wedged(tmp)
    scenario_namespace(tmp)
    scenario_concurrent(tmp)

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
