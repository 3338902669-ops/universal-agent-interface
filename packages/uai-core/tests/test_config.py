"""配置与路径：一份 seats.yml 换机器只需改 UAI_HOME。"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from uai_core import config, paths


def test_uai_home_env(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UAI_HOME", str(tmp_path / "home"))
    assert paths.uai_home() == (tmp_path / "home").resolve()
    assert paths.hub_lock().name == "hub.lock"
    assert paths.seat_lock("dsh").name == "seat-dsh.lock"


def test_default_config_roundtrip(tmp_path: Path) -> None:
    target = tmp_path / "seats.yml"
    cfg = config.default_config()
    cfg["seats"] = [{"id": "dsh", "adapter": "a.py", "enabled": True,
                     "tags": ["orchestrate"]}]
    path = config.save(cfg, target)
    assert path.exists()
    loaded = config.load(target)
    assert loaded["seats"][0]["id"] == "dsh"
    assert config.validate(loaded) == []


def test_validate_catches_duplicates_and_empty(tmp_path: Path) -> None:
    cfg = config.default_config()
    cfg["seats"] = [{"id": "dsh", "adapter": "a"}, {"id": "dsh", "adapter": "b"},
                    {"id": "", "adapter": "c"}, {"id": "x", "enabled": True}]
    problems = config.validate(cfg)
    assert any("duplicate" in p for p in problems)
    assert any("empty id" in p for p in problems)
    assert any("neither adapter nor command" in p for p in problems)


def test_seats_from_detection_enables_only_ready_with_adapter() -> None:
    report = {"results": [
        {"id": "a", "state": "ready", "adapter": "a.py", "capability_tags": ["x"]},
        {"id": "b", "state": "ready", "adapter": None},
        {"id": "c", "state": "auth_failed", "adapter": "c.py"},
    ]}
    seats = config.seats_from_detection(report)
    enabled = [s["id"] for s in seats if s["enabled"]]
    assert enabled == ["a"]
    assert seats[2]["state_at_detect"] == "auth_failed"
