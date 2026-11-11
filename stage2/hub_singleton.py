"""中枢单例守卫（A-09）：防止两个 Hub 在同一命名空间内同时派发。

背景（待完善项 A-09）：当前没有多 Hub 协调设计。围栏只防"旧结果覆盖新的"，
**不防"同一任务被派两次"**——两个中枢各自都认为自己是权威，会各派一份。

本模块给的是最小可用语义（不是共识协议）：

  1. 一个命名空间同一时刻只允许一个中枢持有写权；
  2. 租约带 TTL，持有者必须周期性 heartbeat；
  3. 持有者进程已死或 TTL 过期 → 允许接管，但**必须留下 takeover 记录**（不静默抢占）；
  4. 拿不到锁的一方**必须拒绝派发**，而不是降级为只读后继续派。

边界：这是**单机文件锁**，不是分布式共识。跨机部署需要换成 etcd/Consul/Redis 之类，
但语义（TTL + 心跳 + 接管留痕 + 拿不到就不派）不变。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path


class SingletonError(RuntimeError):
    pass


@dataclass
class Holder:
    pid: int
    namespace: str
    acquired_at: float
    heartbeat_at: float
    generation: int = 1
    history: list = field(default_factory=list)

    def as_record(self) -> dict:
        return {
            "pid": self.pid,
            "namespace": self.namespace,
            "acquired_at": self.acquired_at,
            "heartbeat_at": self.heartbeat_at,
            "generation": self.generation,
        }


def _pid_alive(pid: int) -> bool:
    """进程存活判定。

    注意：Windows 上 `os.kill(pid, 0)` 会**终止**目标进程（Python 文档明写），
    必须走 OpenProcess/GetExitCodeProcess，否则测试会把自己杀掉。
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            QUERY_LIMITED = 0x1000   # PROCESS_QUERY_LIMITED_INFORMATION
            STILL_ACTIVE = 259
            h = ctypes.windll.kernel32.OpenProcess(QUERY_LIMITED, False, pid)
            if not h:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            if not ok:
                return False
            return code.value == STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)          # POSIX：0 号信号只探测
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True

class HubSingleton:
    def __init__(self, path: str | os.PathLike, *, namespace: str = "default",
                 ttl_s: float = 30.0):
        self.path = Path(path)
        self.namespace = namespace
        self.ttl_s = float(ttl_s)
        self._held = False
        self.holder: Holder | None = None
        self.takeover_events: list[dict] = []

    # ---------- 内部 ----------
    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)          # 原子替换

    def _is_stale(self, rec: dict) -> tuple[bool, str]:
        if rec.get("namespace") != self.namespace:
            return True, "namespace_changed"
        if not _pid_alive(int(rec.get("pid", 0))):
            return True, "holder_pid_dead"
        age = time.time() - float(rec.get("heartbeat_at", 0))
        if age > self.ttl_s:
            return True, "ttl_expired"
        return False, "alive"

    # ---------- 对外 ----------
    def status(self) -> dict:
        rec = self._read()
        if not rec:
            return {"held": False, "holder": None}
        stale, why = self._is_stale(rec)
        return {"held": True, "holder": rec, "stale": stale, "why": why}

    def acquire(self) -> tuple[bool, dict]:
        """抢占写权。拿不到就返回 False —— 调用方必须拒绝派发。"""
        rec = self._read()
        if rec is None:
            holder = Holder(pid=os.getpid(), namespace=self.namespace,
                            acquired_at=time.time(), heartbeat_at=time.time())
            self._write(holder.as_record())
            self.holder = holder
            self._held = True
            return True, holder.as_record()

        stale, why = self._is_stale(rec)
        if not stale:
            return False, {"reason": "held_by_other", "holder": rec, "why": why}

        gen = int(rec.get("generation", 1)) + 1
        holder = Holder(pid=os.getpid(), namespace=self.namespace,
                        acquired_at=time.time(), heartbeat_at=time.time(),
                        generation=gen)
        ev = {"event": "takeover", "reason": why, "from": rec, "to": holder.as_record(),
              "ns": self.namespace, "ts": time.time()}
        self._write(holder.as_record())
        self.holder = holder
        self._held = True
        self.takeover_events.append(ev)
        return True, ev

    def heartbeat(self) -> dict:
        if not self._held or not self.holder:
            raise SingletonError("not_holding")
        rec = self._read()
        if rec is None or int(rec.get("pid", -1)) != os.getpid():
            self._held = False
            raise SingletonError("lost_ownership")
        self.holder.heartbeat_at = time.time()
        self._write(self.holder.as_record())
        return {"ok": True, "heartbeat_at": self.holder.heartbeat_at}

    def release(self) -> None:
        rec = self._read()
        if rec and int(rec.get("pid", -1)) == os.getpid():
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self._held = False
        self.holder = None

    @property
    def held(self) -> bool:
        return self._held

    def assert_can_dispatch(self) -> None:
        """派发前的硬门：没有写权就抛错，绝不降级继续派。"""
        if not self._held:
            raise SingletonError("no_write_authority: refusing to dispatch")
