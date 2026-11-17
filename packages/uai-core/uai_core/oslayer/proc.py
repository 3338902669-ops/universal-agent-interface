"""进程发现/存活/终止：psutil 实现，替代 Get-CimInstance + PowerShell。

为什么换：旧实现靠 powershell -Command 数进程，一是慢（每次约 1s），
二是**会把自己数进去**——检查脚本的命令行里带着匹配用的模式字符串，
于是 healthcheck 一度报「监督进程 count=2」。psutil 不 spawn 任何子进程，
这一类自匹配 bug 从根上消失。
"""

from __future__ import annotations

from typing import Iterable, Iterator

import psutil


def iter_processes() -> Iterator[dict]:
    """遍历当前可见进程：{pid, name, cmdline(list), cmd(str)}。"""
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            info = p.info
            cmdline = info.get("cmdline") or []
            yield {
                "pid": int(info.get("pid") or 0),
                "name": info.get("name") or "",
                "cmdline": list(cmdline),
                "cmd": " ".join(cmdline),
            }
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def find(*, contains: Iterable[str] = (), name: Iterable[str] = (),
         exclude_contains: Iterable[str] = (),
         exclude_pids: Iterable[int] = ()) -> list[dict]:
    """按命令行子串 / 进程名匹配。全部条件满足才算命中。

    contains 里每一条都必须出现在命令行里；exclude_contains 命中任意一条即排除。
    """
    contains = tuple(contains)
    names = tuple(n.lower() for n in name)
    excludes = tuple(exclude_contains)
    skip = {int(x) for x in exclude_pids}
    out = []
    for row in iter_processes():
        if row["pid"] in skip:
            continue
        if names and row["name"].lower() not in names:
            continue
        cmd = row["cmd"]
        if contains and not all(token in cmd for token in contains):
            continue
        if excludes and any(token in cmd for token in excludes):
            continue
        out.append(row)
    return out


def count(**kwargs) -> int:
    return len(find(**kwargs))


def alive(pid: int) -> bool:
    """存活判定走 psutil（跨平台一致，且不会像 os.kill(pid,0) 那样在 Windows 上杀进程）。"""
    try:
        return psutil.Process(int(pid)).is_running()
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return False
    except psutil.AccessDenied:
        return True
    except Exception:
        return False


def kill(pid: int, *, timeout: float = 8.0) -> bool:
    """终止进程（含子进程）。返回是否已不在。"""
    try:
        parent = psutil.Process(int(pid))
    except (psutil.NoSuchProcess, ValueError, TypeError):
        return True
    except psutil.AccessDenied:
        return False
    victims = []
    try:
        victims = parent.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    for victim in victims + [parent]:
        try:
            victim.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    gone, still = psutil.wait_procs(victims + [parent], timeout=timeout)
    for victim in still:
        try:
            victim.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    gone2, still2 = psutil.wait_procs(still, timeout=3.0)
    return not still2
