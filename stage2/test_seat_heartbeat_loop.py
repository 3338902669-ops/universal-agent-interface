"""席位心跳循环回归（常驻面锁死的一部分）。

2026-11-17 实测踩到的两个真问题，都属于"进程活着但什么都不做"：

  1. 心跳循环里只要有一处缩进错位，循环体跑一次就 `return` —— 进程在、日志静、
     中枢 8 秒后把席位判 stale，而且**再也不会恢复**（实测 80s 无动作）。
  2. 中枢重启后席位内存里 `registered` 仍是 True；旧实现只在收到 `must_register`
     时重新注册，而旧中枢对未知会话回的是 `unauthorized` —— 于是永远不重注册。

这两条都拿不到"看一眼进程在不在"能发现的证据，所以必须用假 NATS 直接驱动循环。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

PROTO = Path(__file__).resolve().parent.parent / "prototype"
sys.path.insert(0, str(PROTO))

import interface as IF                  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-56s %s" % ("PASS" if cond else "FAIL", name, detail))


class FakeReply:
    def __init__(self, payload):
        self.data = json.dumps(payload).encode("utf-8")


class FakeNC:
    """只实现心跳循环用到的那一点点：is_connected / request。"""

    def __init__(self, reply):
        self.is_connected = True
        self.reply = reply
        self.calls = 0

    async def request(self, subject, payload, timeout=2):
        self.calls += 1
        if isinstance(self.reply, Exception):
            raise self.reply
        return FakeReply(self.reply)


def build(tmp: Path):
    tmp.mkdir(parents=True, exist_ok=True)
    tok = tmp / "t.token"; tok.write_text("dummy-token", encoding="utf-8")
    proc = IF.InterfaceProcess(
        base=tmp,
        interface_id="unit",
        token_file=tok,
        nats_url="nats://127.0.0.1:1",
        heartbeat_s=0.02,
        task_heartbeat_s=1.0,
        event_timeout_s=0.5,
        max_concurrency=1,
        disable_heartbeat=False,
        suppress_echo=True,
        executor="test",
        runtime_executable=None,
    )
    proc.register_calls = 0

    async def fake_register():
        proc.register_calls += 1
        proc.registered = True
        return True

    proc.register = fake_register
    proc.registered = True
    return proc


async def drive(proc, seconds=0.35):
    task = asyncio.create_task(proc.heartbeat_loop())
    await asyncio.sleep(seconds)
    proc.stop_event.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="uai-seat-hb-"))

    # 1) 正常 ok：循环必须**持续**发心跳，不是跑一次就退出
    proc = build(tmp / "ok")
    nc = FakeNC({"ok": True, "active": True})
    proc.nc = nc
    await drive(proc)
    check("心跳循环持续运行（不是跑一次就 return）", nc.calls >= 3,
          "calls=%d" % nc.calls)

    # 2) 中枢说 unknown_session：必须重新注册
    proc2 = build(tmp / "unknown")
    nc2 = FakeNC({"ok": False, "reason": "unknown_session", "must_register": True})
    proc2.nc = nc2
    await drive(proc2, 0.25)
    check("收到 must_register/unknown_session 会重新注册", proc2.register_calls >= 1,
          "calls=%d" % proc2.register_calls)
    check("重注册后仍继续发心跳", nc2.calls >= 2, "calls=%d" % nc2.calls)

    # 3) 中枢只回 unauthorized（旧中枢行为）：也要重注册，不能干等
    proc3 = build(tmp / "unauth")
    nc3 = FakeNC({"ok": False, "reason": "unauthorized"})
    proc3.nc = nc3
    await drive(proc3, 0.25)
    check("中枢只回 unauthorized 也会触发重注册", proc3.register_calls >= 1,
          "calls=%d" % proc3.register_calls)

    # 4) NATS 断了：循环不能死，也不能刷屏
    proc4 = build(tmp / "offline")
    nc4 = FakeNC({"ok": True})
    nc4.is_connected = False
    proc4.nc = nc4
    await drive(proc4, 0.3)
    check("NATS 未连接时循环继续但不再发请求", nc4.calls == 0,
          "calls=%d" % nc4.calls)
    log = (proc4.state.connection_path).read_text(encoding="utf-8").splitlines()
    skipped = [l for l in log if "heartbeat_skipped" in l]
    check("未连接时留痕且限频（不会每 20ms 刷一行）", 1 <= len(skipped) <= 3,
          "lines=%d" % len(skipped))

    # 5) 请求抛异常：记录错误但不退出循环
    proc5 = build(tmp / "err")
    nc5 = FakeNC(RuntimeError("boom"))
    nc5.is_connected = True
    proc5.nc = nc5
    await drive(proc5, 0.25)
    check("心跳异常后循环继续重试", nc5.calls >= 3, "calls=%d" % nc5.calls)
    log5 = (proc5.state.connection_path).read_text(encoding="utf-8")
    check("心跳异常有 error 记录", "heartbeat_error" in log5, "")

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
