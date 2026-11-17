"""路径解析：唯一来源是 UAI_HOME，其他一律从它派生。

历史问题：旧代码里散落着 C:\\Users\\33389\\... 这类绝对路径，换机器就废。
这里把它们收敛成一处可改。
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_HOME_DIRNAME = ".uai"


def uai_home() -> Path:
    """UAI_HOME 环境变量优先；否则 ~/.uai。"""
    raw = os.environ.get("UAI_HOME", "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / DEFAULT_HOME_DIRNAME).resolve()


def ensure_home() -> Path:
    home = uai_home()
    for sub in ("", "data", "logs", "run", "runtime"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def base_dir() -> Path:
    """数据面（账本所在）。默认 UAI_HOME/base，可用 UAI_BASE 覆盖。"""
    raw = os.environ.get("UAI_BASE", "").strip()
    return Path(raw).expanduser().resolve() if raw else uai_home() / "base"


def data_dir() -> Path:
    return base_dir() / "data"


def logs_dir() -> Path:
    return uai_home() / "logs"


def run_dir() -> Path:
    return uai_home() / "run"


def seats_file() -> Path:
    return uai_home() / "seats.yml"


def hub_lock() -> Path:
    return data_dir() / "hub.lock"


def seat_lock(seat_id: str) -> Path:
    return data_dir() / ("seat-%s.lock" % seat_id)


def ledger_file() -> Path:
    return data_dir() / "ledger.jsonl"


def heartbeats_file() -> Path:
    return data_dir() / "heartbeats.jsonl"
