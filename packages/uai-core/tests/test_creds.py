"""creds / env_keys 的回归测试（由 codex 席位审代码发现的两个缺口）。"""

from __future__ import annotations

import os

from uai_core.cli import _as_env_keys
from uai_core.config import validate
from uai_core.oslayer import creds


def test_env_keys_string_is_not_split_into_chars() -> None:
    assert _as_env_keys({"env_keys": "DEEPSEEK_API_KEY"}) == ("DEEPSEEK_API_KEY",)


def test_env_keys_list_is_trimmed() -> None:
    assert _as_env_keys({"env_keys": [" A ", "", None, "B"]}) == ("A", "B")


def test_config_rejects_env_keys_as_bare_string() -> None:
    problems = validate(
        {"processes": [{"name": "hub", "command": ["x"], "env_keys": "DEEPSEEK_API_KEY"}]}
    )
    assert any("env_keys" in p for p in problems), problems


def test_config_accepts_env_keys_list() -> None:
    problems = validate(
        {"processes": [{"name": "hub", "command": ["x"], "env_keys": ["A", "B"]}]}
    )
    assert not any("env_keys" in p for p in problems), problems


def test_whitespace_env_falls_back_to_registry(monkeypatch) -> None:
    monkeypatch.setenv("UAI_TEST_KEY", "   ")
    monkeypatch.setattr(creds, "_from_registry", lambda names: {"UAI_TEST_KEY": "from-registry"})
    injected, diag = creds.resolve(("UAI_TEST_KEY",))
    assert injected["UAI_TEST_KEY"] == "from-registry"
    assert "registry" in diag["source"]


def test_diag_never_contains_values(monkeypatch) -> None:
    monkeypatch.setenv("UAI_TEST_SECRET", "super-secret-value")
    injected, diag = creds.resolve(("UAI_TEST_SECRET",))
    assert injected["UAI_TEST_SECRET"] == "super-secret-value"
    assert "super-secret-value" not in str(diag)


def test_injected_value_is_not_trimmed(monkeypatch) -> None:
    """宿主已有值优先且不覆盖：注入必须是原值（codex 审代码 FAIL 第 1 条）。"""
    monkeypatch.setenv("UAI_TEST_RAW", "  keep-spaces  ")
    injected, _ = creds.resolve(("UAI_TEST_RAW",))
    assert injected["UAI_TEST_RAW"] == "  keep-spaces  "


def test_env_key_names_with_equals_or_nul_are_dropped() -> None:
    """非法变量名（含 = 或 NUL）必须丢弃（FAIL 第 3 条）。"""
    assert _as_env_keys({"env_keys": ["A", "B=C", "D\x00E", "  "]}) == ("A",)