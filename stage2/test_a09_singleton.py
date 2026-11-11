"""A-09 自检：中枢单例守卫。

四个关键行为：
  1. 首个实例拿到写权；
  2. 第二个实例被拒，且能报出持有者；**拿不到写权不得派发**；
  3. 持有者心跳续期；
  4. 持有者已死 / TTL 过期 → 允许接管，但必须留 takeover 记录。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time

import hub_singleton as HS

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-60s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="uai-singleton-")
    path = os.path.join(tmp, "hub.lock")

    a = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okA, infoA = a.acquire()
    check("A-09 首个实例取得写权", okA is True, json.dumps(infoA)[:60])
    check("A-09 held=True", a.held is True, "")

    b = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okB, infoB = b.acquire()
    check("A-09 第二个实例被拒", okB is False, infoB.get("why", ""))
    check("A-09 拒绝时能报出持有者 pid",
          infoB.get("holder", {}).get("pid") == os.getpid(), str(infoB.get("holder", {}).get("pid")))

    # 拿不到写权 → 拒绝派发
    try:
        b.assert_can_dispatch()
    except HS.SingletonError as e:
        check("A-09 无写权时拒绝派发（不降级继续派）", "refusing to dispatch" in str(e), str(e)[:50])
    else:
        check("A-09 无写权时拒绝派发（不降级继续派）", False, "竟然允许派发")

    # 心跳
    hb = a.heartbeat()
    check("A-09 持有者心跳成功", hb.get("ok") is True, "")

    # 非持有者心跳应失败
    try:
        b.heartbeat()
    except HS.SingletonError as e:
        check("A-09 非持有者心跳被拒", "not_holding" in str(e), str(e)[:40])
    else:
        check("A-09 非持有者心跳被拒", False, "未抛错")

    # 模拟持有者死亡：改成不存在的 pid
    rec = json.loads(open(path, encoding="utf-8").read())
    rec["pid"] = 999999
    open(path, "w", encoding="utf-8").write(json.dumps(rec))
    c = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okC, infoC = c.acquire()
    check("A-09 持有者进程已死 → 允许接管", okC is True, str(infoC.get("reason") if isinstance(infoC, dict) else infoC))
    check("A-09 接管留下事件记录",
          bool(c.takeover_events) and c.takeover_events[0]["reason"] == "holder_pid_dead",
          str(c.takeover_events[0]["reason"] if c.takeover_events else None))
    check("A-09 接管后 generation 递增",
          c.holder is not None and c.holder.generation == 2, str(c.holder.generation if c.holder else None))

    # TTL 过期接管
    rec = json.loads(open(path, encoding="utf-8").read())
    rec["heartbeat_at"] = time.time() - 3600
    open(path, "w", encoding="utf-8").write(json.dumps(rec))
    d = HS.HubSingleton(path, namespace="lab", ttl_s=30)
    okD, infoD = d.acquire()
    check("A-09 TTL 过期 → 允许接管", okD is True, str(infoD.get("reason") if isinstance(infoD, dict) else infoD))

    # 命名空间不同 → 视为 stale
    e = HS.HubSingleton(path, namespace="other", ttl_s=30)
    okE, infoE = e.acquire()
    check("A-09 命名空间不同 → 不共用同一把锁", okE is True, "")

    # 释放
    e.release()
    st = HS.HubSingleton(path, namespace="other", ttl_s=30).status()
    check("A-09 释放后锁文件消失", st["held"] is False, str(st))

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
