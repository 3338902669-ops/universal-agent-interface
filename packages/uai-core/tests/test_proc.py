"""进程层：psutil 取代 Get-CimInstance。"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from uai_core.oslayer import proc


def test_count_and_find_self_marker() -> None:
    marker = "uai-core-proc-test-%d" % os.getpid()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])
    try:
        time.sleep(0.6)
        rows = proc.find(contains=(marker,))
        assert [r["pid"] for r in rows] == [child.pid]
        assert proc.count(contains=(marker,)) == 1
    finally:
        proc.kill(child.pid)


def test_exclude_contains_filters_noise() -> None:
    marker = "uai-core-exclude-%d" % os.getpid()
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", marker])
    try:
        time.sleep(0.6)
        assert proc.count(contains=(marker,)) == 1
        assert proc.count(contains=(marker,), exclude_contains=("no-such-token-xyz",)) == 1
        assert proc.count(contains=(marker,), exclude_contains=(marker,)) == 0
    finally:
        proc.kill(child.pid)


def test_alive_and_kill() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    time.sleep(0.4)
    assert proc.alive(child.pid) is True
    assert proc.kill(child.pid) is True
    time.sleep(0.3)
    assert proc.alive(child.pid) is False
    assert proc.alive(999999) is False


def test_find_self_by_executable_path() -> None:
    """按解释器路径找自己：跨平台稳定（进程名在 Linux 上是 python3/python3.13）。"""
    rows = proc.find(contains=(sys.executable,), exclude_pids=(0,))
    assert any(r["pid"] == os.getpid() for r in rows)
