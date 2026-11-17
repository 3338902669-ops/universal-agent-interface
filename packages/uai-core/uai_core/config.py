"""席位与监督配置（seats.yml）：一份文件描述「谁在跑、怎么跑」。

原则：配置里只放**跨平台的东西**（命令数组、环境、锁名），不放 PowerShell 或绝对路径。
缺省值全部来自 paths，因此同一份 seats.yml 换机器只需改 UAI_HOME。
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from . import paths

SCHEMA = "uai.seats/v1"


def default_config() -> dict:
    return {
        "schema": SCHEMA,
        "hub": {"nats_url": os.environ.get("UAI_NATS_URL", "nats://127.0.0.1:4223")},
        "supervisor": {"restart_backoff_s": 5.0, "max_backoff_s": 60.0},
        "seats": [],
        "processes": [],
    }


def load(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else paths.seats_file()
    if not p.exists():
        return default_config()
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    cfg = default_config()
    cfg.update(raw)
    if not isinstance(cfg.get("seats"), list):
        cfg["seats"] = []
    if not isinstance(cfg.get("processes"), list):
        cfg["processes"] = []
    return cfg


def save(cfg: dict, path: str | os.PathLike | None = None) -> Path:
    p = Path(path) if path else paths.seats_file()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return p


def validate(cfg: dict) -> list[str]:
    problems = []
    seen = set()
    for seat in cfg.get("seats") or []:
        sid = str(seat.get("id") or "").strip()
        if not sid:
            problems.append("seat with empty id")
            continue
        if sid in seen:
            problems.append("duplicate seat id: " + sid)
        seen.add(sid)
        if seat.get("enabled", True) and not seat.get("adapter") and not seat.get("command"):
            problems.append("seat %s has neither adapter nor command" % sid)
    for i, proc in enumerate(cfg.get("processes") or []):
        cmd = proc.get("command")
        if not cmd or not isinstance(cmd, list):
            problems.append("processes[%d].command must be a list" % i)
    return problems


def seats_from_detection(report: dict) -> list[dict]:
    """把检测结果变成席位草案：只把「真正可用」的放进 enabled。"""
    seats = []
    for row in report.get("results") or []:
        state = row.get("state")
        seats.append({
            "id": row.get("id"),
            "label": row.get("label"),
            "vendor": row.get("vendor"),
            "state_at_detect": state,
            "version": row.get("version"),
            "tags": list(row.get("capability_tags") or []),
            "adapter": row.get("adapter"),
            "enabled": state in ("ready", "running") and bool(row.get("adapter")),
        })
    return seats
