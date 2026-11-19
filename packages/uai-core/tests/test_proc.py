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
    try:
        child.wait(timeout=10)   # 收尸：否则 Linux 上 PID 变僵尸，alive() 会看到壳
    except Exception:
        pass
    time.sleep(0.3)
    assert proc.alive(child.pid) is False
    assert proc.alive(999999) is False


def test_find_includes_own_process() -> None:
    """枚举必须包含自己（不靠命令行里恰好出现某个路径——venv 里 sys.executable
    常是软链，cmdline 里是另一个等价路径，按它匹配在 Linux 上会落空）。"""
    rows = proc.find()
    assert any(r["pid"] == os.getpid() for r in rows)


def test_name_prefix_keeps_python_only() -> None:
    """监督/席位识别用的是 name_prefix=('python',)：必须真的只留下 python 类进程。"""
    rows = proc.find(name_prefix=("python",))
    assert any(r["pid"] == os.getpid() for r in rows)
    assert all(r["name"].lower().startswith("python") for r in rows)
