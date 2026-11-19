"""中枢单例守卫（A-09）——实现已迁到跨平台核心 uai_core.oslayer.lock。

为什么迁：原型阶段这份代码里混着 Windows 专属 API（msvcrt / ctypes OpenProcess），
以及"读 JSON 看 pid 活不活"的旧语义。2026-11-17 起互斥事实统一由 uai_core 的
OS 独占锁裁决，本模块只保留**入口名**，避免改动 prototype/hub.py 与既有测试。

行为（已由 test_a09_singleton.py 28 条 + packages/uai-core/tests/test_lock.py 覆盖）：
  1. OS 独占锁互斥，不是元数据猜测；
  2. 持锁进程死亡 → 允许接管，takeover 记录落盘，generation 单调递增；
  3. TTL 只用于告警，不用于从活进程手里夺锁；
  4. 拿不到写权必须拒绝派发（assert_can_dispatch 抛错）。
"""

from __future__ import annotations

from uai_core.oslayer.lock import (  # noqa: F401
    GENESIS,
    FileLock,
    Holder,
    SingletonError,
    pid_alive,
)

# 兼容旧入口名
HubSingleton = FileLock
_pid_alive = pid_alive  # resident_health / drill 曾直接调它
