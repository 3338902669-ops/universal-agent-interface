"""跨平台独占锁：同一把锁同一时刻只允许一个持有者。

语义（与 UAI 的 A-09 一致，已由 28 个用例覆盖）：
  1. 互斥事实来自操作系统独占锁（Windows msvcrt.locking / POSIX flock），
     不是"读 JSON 看 pid 活不活"——后者两个进程能同时读到没人持有，然后都写。
  2. 持锁进程死亡 → 内核释放锁 → 允许接管，但必须留下 takeover 记录
     （写进锁文件的 history，持有者死了也查得到），generation 单调递增。
  3. TTL 只用于告警（stale/wedged），**不用于从活进程手里夺锁**。
  4. 拿不到锁的一方必须拒绝干活（assert_can_dispatch 抛错），不降级。

边界：单机文件锁，不是分布式共识。跨机需要换成 etcd/Consul/Redis，语义不变。
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

GENESIS = "0" * 64
_LOCK_BYTE = b"\x2a"


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
            "history": list(self.history),
        }


def pid_alive(pid: int) -> bool:
    """进程存活判定。

    注意：Windows 上 os.kill(pid, 0) 会**终止**目标进程（Python 文档明写），
    必须走 OpenProcess/GetExitCodeProcess，否则会把自己杀掉。
    """
    pid = int(pid or 0)
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            QUERY_LIMITED = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(QUERY_LIMITED, False, pid)
            if not handle:
                return False
            code = ctypes.c_ulong()
            ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(handle)
            if not ok:
                return False
            return code.value == STILL_ACTIVE
        except Exception:
            return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return True


def _open_lockfile(path: Path) -> int:
    """打开/创建 OS 锁文件，并保证至少有 1 字节可锁区。"""
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(str(path), flags, 0o644)
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, _LOCK_BYTE)
    except OSError:
        pass
    return fd


def _os_try_lock(fd: int) -> bool:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _os_unlock(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        return
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


class FileLock:
    """同一 path 上同一时刻只允许一个写者。

        guard = FileLock(paths.hub_lock(), namespace="uai-hub")
        ok, info = guard.acquire()
        guard.assert_can_dispatch()
    """

    def __init__(self, path, *, namespace: str = "default", ttl_s: float = 30.0):
        self.path = Path(path)
        self.lock_path = Path(str(path) + ".oslock")
        self.namespace = namespace
        self.ttl_s = float(ttl_s)
        self._fd: int | None = None
        self._held = False
        self.holder: Holder | None = None
        self.takeover_events: list[dict] = []

    # ---------- 内部 ----------
    def _read(self) -> dict | None:
        if not self.path.exists():
            return None
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _write(self, rec: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.path)

    def _clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # ---------- 对外 ----------
    def status(self) -> dict:
        """只读探针：不改变任何状态。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = _open_lockfile(self.lock_path)
        free = _os_try_lock(fd)
        if free:
            _os_unlock(fd)
            os.close(fd)
            rec = self._read()
            if not rec:
                return {"held": False, "holder": None, "stale": False, "why": "none"}
            return {"held": False, "holder": rec, "stale": True,
                    "why": "metadata_leftover",
                    "leftover_pid_alive": pid_alive(int(rec.get("pid", 0) or 0))}
        os.close(fd)
        rec = self._read()
        if not rec:
            return {"held": True, "holder": None, "stale": False,
                    "why": "os_lock_held_no_metadata"}
        why, stale = "os_lock_held", False
        if rec.get("namespace") != self.namespace:
            why = "namespace_mismatch"
        elif time.time() - float(rec.get("heartbeat_at", 0) or 0) > self.ttl_s:
            why, stale = "ttl_expired_wedged", True
        return {"held": True, "holder": rec, "stale": stale, "why": why}

    def acquire(self) -> tuple[bool, dict]:
        """抢占写权。拿不到返回 False——调用方必须拒绝干活。"""
        if self._held and self._fd is not None:
            return True, {"reason": "already_held", **(self.holder.as_record() if self.holder else {})}

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = _open_lockfile(self.lock_path)
        if not _os_try_lock(fd):
            os.close(fd)
            rec = self._read() or {}
            return False, {"reason": "held_by_other", "holder": rec,
                           "why": "os_lock_held",
                           "holder_alive": pid_alive(int(rec.get("pid", 0) or 0))}

        prev = self._read()
        now = time.time()
        history = list((prev or {}).get("history") or [])
        gen = int((prev or {}).get("generation", 0) or 0) + 1
        reason = "fresh" if not prev else "takeover"
        if prev:
            ev = {
                "event": "takeover",
                "reason": "os_lock_released",
                "namespace_changed": prev.get("namespace") != self.namespace,
                "from_namespace": prev.get("namespace"),
                "same_process": int(prev.get("pid", -1)) == os.getpid(),
                "previous": {k: prev.get(k) for k in
                             ("pid", "namespace", "acquired_at", "heartbeat_at", "generation")},
                "previous_pid_alive": pid_alive(int(prev.get("pid", 0) or 0)),
                "to_pid": os.getpid(),
                "generation": gen,
                "ns": self.namespace,
                "ts": now,
            }
            history.append(ev)
            self.takeover_events.append(ev)

        holder = Holder(pid=os.getpid(), namespace=self.namespace,
                        acquired_at=now, heartbeat_at=now,
                        generation=gen, history=history)
        self._write(holder.as_record())
        self._fd = fd
        self._held = True
        self.holder = holder
        return True, {"reason": reason, "generation": gen, "previous": prev}

    def heartbeat(self) -> dict:
        if not self._held or self._fd is None or not self.holder:
            raise SingletonError("not_holding")
        rec = self._read()
        if rec is None or int(rec.get("pid", -1)) != os.getpid():
            self._held = False
            raise SingletonError("lost_ownership")
        self.holder.heartbeat_at = time.time()
        self._write(self.holder.as_record())
        return {"ok": True, "heartbeat_at": self.holder.heartbeat_at}

    def release(self) -> None:
        """正常释放：清元数据 → 放 OS 锁 → 关 fd。"""
        if self._fd is not None:
            try:
                self._clear()
                _os_unlock(self._fd)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        self._held = False
        self.holder = None

    def abandon(self) -> None:
        """故障注入：模拟进程被强杀——内核释放 OS 锁，元数据留在盘上。"""
        if self._fd is not None:
            try:
                _os_unlock(self._fd)
            finally:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        self._held = False
        self.holder = None

    @property
    def held(self) -> bool:
        return self._held

    def assert_can_dispatch(self) -> None:
        """干活前的硬门：没有写权就抛错，绝不降级继续。"""
        if not self._held:
            raise SingletonError("no_write_authority: refusing to dispatch")


# 兼容旧名（stage2/hub_singleton.py 曾用这个名字）
HubSingleton = FileLock
