"""Single-host Hub guard using an OS-held file lock, not distributed consensus.

All participants must use the same path on a local filesystem and this protocol.
The sidecar lock file must not be removed while any participant is running.
An expired or otherwise unverifiable live holder is never forcibly replaced.
A dispatch check is a point-in-time check, not fencing of external side effects.
"""
from __future__ import annotations

import json
import os
import time
import uuid
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
    token: str = field(default_factory=lambda: uuid.uuid4().hex)

    def as_record(self) -> dict:
        return {"pid": self.pid, "namespace": self.namespace,
                "acquired_at": self.acquired_at, "heartbeat_at": self.heartbeat_at,
                "generation": self.generation, "token": self.token}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return True  # malformed or unknown: fail closed
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.GetExitCodeProcess.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() != 87  # only ERROR_INVALID_PARAMETER proves absent
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


class HubSingleton:
    def __init__(self, path: str | os.PathLike, *, namespace: str = "default", ttl_s: float = 30.0):
        self.path = Path(path)
        self.namespace = namespace
        self.ttl_s = float(ttl_s)
        self._held = False
        self._lock_file = None
        self._owner_pid = None
        self.holder: Holder | None = None
        self.takeover_events: list[dict] = []

    def _read(self) -> dict | None:
        try:
            with self.path.open(encoding="utf-8") as stream:
                rec = json.load(stream)
            if not isinstance(rec, dict):
                raise ValueError("record is not an object")
            return rec
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise SingletonError("invalid_holder_record") from exc

    def _write(self, rec: dict) -> None:
        tmp = self.path.with_name(self.path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            tmp.write_text(json.dumps(rec, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _try_lock(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = open(str(self.path) + ".mutex", "a+b")
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            stream.close()
            return False
        self._lock_file = stream
        self._owner_pid = os.getpid()
        return True

    def _unlock(self) -> None:
        stream, self._lock_file = self._lock_file, None
        if stream is not None:
            # A forked child must not unlock the parent's open-file-description lock.
            if self._owner_pid == os.getpid():
                try:
                    if os.name == "nt":
                        import msvcrt
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                finally:
                    stream.close()
            else:
                stream.close()
        self._owner_pid = None

    def _is_stale(self, rec: dict) -> tuple[bool, str]:
        try:
            pid = int(rec["pid"])
            heartbeat = float(rec["heartbeat_at"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise SingletonError("invalid_holder_record")
        if not _pid_alive(pid):
            return True, "holder_pid_dead"
        if time.time() - heartbeat > self.ttl_s:
            return False, "ttl_expired_live_holder"
        return False, "alive"

    def status(self) -> dict:
        rec = self._read()
        if rec is None:
            return {"held": False, "holder": None}
        stale, why = self._is_stale(rec)
        return {"held": True, "holder": rec, "stale": stale, "why": why}

    def acquire(self) -> tuple[bool, dict]:
        if self._held:
            return False, {"reason": "already_holding"}
        if not self._try_lock():
            try:
                rec = self._read()
            except SingletonError:
                rec = None
            return False, {"reason": "held_by_other", "holder": rec, "why": "mutex_locked"}
        try:
            rec = self._read()
            if rec is not None:
                stale, why = self._is_stale(rec)
                if not stale:
                    self._unlock()
                    return False, {"reason": "held_by_other", "holder": rec, "why": why}
                generation = int(rec.get("generation", 1)) + 1
            else:
                generation = 1
            now = time.time()
            holder = Holder(pid=os.getpid(), namespace=self.namespace, acquired_at=now,
                            heartbeat_at=now, generation=generation)
            self._write(holder.as_record())
            self.holder, self._held = holder, True
            if rec is None:
                return True, holder.as_record()
            event = {"event": "takeover", "reason": why, "from": rec,
                     "to": holder.as_record(), "ns": self.namespace, "ts": now}
            self.takeover_events.append(event)
            return True, event
        except BaseException:
            self._unlock()
            raise

    def _validate(self) -> None:
        if not self._held or not self.holder or self._owner_pid != os.getpid() or self._lock_file is None:
            raise SingletonError("no_write_authority: refusing to dispatch")
        try:
            rec = self._read()
            valid = (rec is not None and rec == self.holder.as_record() and
                     time.time() - self.holder.heartbeat_at <= self.ttl_s)
        except SingletonError:
            valid = False
        if not valid:
            # Keep the mutex until release() can remove our own record safely.
            # Dispatch and heartbeat fail closed in the meantime.
            self._held = False
            raise SingletonError("lost_ownership: refusing to dispatch")

    def heartbeat(self) -> dict:
        if not self._held:
            raise SingletonError("not_holding")
        self._validate()
        self.holder.heartbeat_at = time.time()
        self._write(self.holder.as_record())
        return {"ok": True, "heartbeat_at": self.holder.heartbeat_at}

    def release(self) -> None:
        try:
            if self.holder and self._lock_file is not None and self._owner_pid == os.getpid():
                try:
                    if self._read() == self.holder.as_record():
                        self.path.unlink(missing_ok=True)
                except SingletonError:
                    pass
        finally:
            self._held = False
            self.holder = None
            self._unlock()

    @property
    def held(self) -> bool:
        try:
            self._validate()
            return True
        except SingletonError:
            return False

    def assert_can_dispatch(self) -> None:
        self._validate()
