"""锁：跨平台独占语义（对应 A-09 的核心行为）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from uai_core.oslayer.lock import FileLock, HubSingleton, SingletonError, pid_alive


def test_first_acquire_wins(tmp_path: Path) -> None:
    guard = FileLock(tmp_path / "a.lock", namespace="t", ttl_s=30)
    ok, info = guard.acquire()
    assert ok is True and info["reason"] == "fresh"
    assert guard.held is True
    assert (tmp_path / "a.lock.oslock").exists()


def test_second_acquire_denied_and_reports_holder(tmp_path: Path) -> None:
    path = tmp_path / "b.lock"
    first = FileLock(path, namespace="t", ttl_s=30)
    assert first.acquire()[0] is True
    second = FileLock(path, namespace="t", ttl_s=30)
    ok, info = second.acquire()
    assert ok is False and info["reason"] == "held_by_other"
    assert info["holder"]["pid"] == os.getpid()
    with pytest.raises(SingletonError):
        second.assert_can_dispatch()
    first.release()


def test_holder_dies_takeover_records_history(tmp_path: Path) -> None:
    path = tmp_path / "c.lock"
    first = FileLock(path, namespace="t", ttl_s=30)
    first.acquire()
    first.abandon()  # 模拟被强杀：内核放锁，元数据留盘
    second = FileLock(path, namespace="t", ttl_s=30)
    ok, info = second.acquire()
    assert ok is True and info["reason"] == "takeover"
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["generation"] == 2
    assert record["history"][0]["event"] == "takeover"
    assert record["history"][0]["previous_pid_alive"] is True  # 同进程，但链路要记
    second.release()


def test_ttl_expiry_never_steals_from_live_holder(tmp_path: Path) -> None:
    path = tmp_path / "d.lock"
    first = FileLock(path, namespace="t", ttl_s=30)
    first.acquire()
    record = json.loads(path.read_text(encoding="utf-8"))
    record["heartbeat_at"] = 0
    path.write_text(json.dumps(record), encoding="utf-8")
    probe = FileLock(path, namespace="t", ttl_s=30)
    ok, info = probe.acquire()
    assert ok is False and info["why"] == "os_lock_held"
    status = probe.status()
    assert status["held"] is True and status["why"] == "ttl_expired_wedged"
    first.release()


def test_heartbeat_and_lost_ownership(tmp_path: Path) -> None:
    path = tmp_path / "e.lock"
    guard = FileLock(path, namespace="t", ttl_s=30)
    guard.acquire()
    assert guard.heartbeat()["ok"] is True
    path.unlink()
    with pytest.raises(SingletonError):
        guard.heartbeat()
    guard.release()


def test_release_clears_and_status_free(tmp_path: Path) -> None:
    path = tmp_path / "f.lock"
    guard = FileLock(path, namespace="t", ttl_s=30)
    guard.acquire()
    guard.release()
    assert guard.held is False
    assert path.exists() is False
    status = FileLock(path, namespace="t", ttl_s=30).status()
    assert status["held"] is False and status["holder"] is None


def test_namespace_change_recorded(tmp_path: Path) -> None:
    path = tmp_path / "g.lock"
    first = FileLock(path, namespace="ns-a", ttl_s=30)
    first.acquire()
    first.abandon()
    second = FileLock(path, namespace="ns-b", ttl_s=30)
    ok, _ = second.acquire()
    assert ok is True
    assert second.takeover_events[0]["namespace_changed"] is True
    second.release()


def test_alias_keeps_old_name() -> None:
    assert HubSingleton is FileLock


def test_pid_alive_self() -> None:
    assert pid_alive(os.getpid()) is True
    assert pid_alive(0) is False


CONCURRENCY = r"""
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from uai_core.oslayer.lock import FileLock
guard = FileLock(sys.argv[2], namespace="t", ttl_s=30)
ok, info = guard.acquire()
print(json.dumps({"pid": os.getpid(), "ok": ok, "reason": info.get("reason")}))
if ok:
    import time; time.sleep(0.4)
    guard.release()
"""


def test_eight_processes_exactly_one_winner(tmp_path: Path) -> None:
    script = tmp_path / "child.py"
    script.write_text(CONCURRENCY, encoding="utf-8")
    pkg_root = str(Path(__file__).resolve().parents[1])
    path = tmp_path / "race.lock"
    procs = [subprocess.Popen([sys.executable, str(script), pkg_root, str(path)],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
             for _ in range(8)]
    winners = []
    for proc in procs:
        out, err = proc.communicate(timeout=60)
        lines = (out or "").strip().splitlines()
        if lines:
            row = json.loads(lines[-1])
            if row.get("ok"):
                winners.append(row["pid"])
    assert len(winners) == 1, "expected exactly one writer, got %r" % (winners,)
