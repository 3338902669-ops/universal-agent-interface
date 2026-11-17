"""三层检测：没装 ≠ 认证失败 ≠ 版本不兼容。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from uai_core.detect import detector


def write_probes(tmp_path: Path, agents: list[dict]) -> Path:
    path = tmp_path / "probes.json"
    path.write_text(json.dumps({"schema": "test", "agents": agents}), encoding="utf-8")
    return path


def test_absent_when_binary_missing(tmp_path: Path) -> None:
    probes = write_probes(tmp_path, [{"id": "ghost", "binaries": ["definitely-not-a-real-binary-xyz"]}])
    row = detector.detect(probes)["results"][0]
    assert row["state"] == detector.STATE_ABSENT
    assert row["binary"] is None


def test_ready_when_version_succeeds(tmp_path: Path) -> None:
    probes = write_probes(tmp_path, [{"id": "py", "binaries": [sys.executable],
                                     "version_args": ["--version"],
                                     "ready_hints": ["Python \\d+\\.\\d+"]}])
    row = detector.detect(probes)["results"][0]
    assert row["state"] == detector.STATE_READY
    assert row["version"] and row["version"].startswith("Python")


def test_auth_failure_is_its_own_state(tmp_path: Path) -> None:
    script = tmp_path / "fake_auth.py"
    script.write_text("import sys; sys.stderr.write('401 Unauthorized: missing api key'); sys.exit(1)", encoding="utf-8")
    probes = write_probes(tmp_path, [{"id": "authy", "binaries": [sys.executable],
                                     "version_args": [str(script)],
                                     "auth_fail_patterns": ["401", "api key"]}])
    row = detector.detect(probes)["results"][0]
    assert row["state"] == detector.STATE_AUTH
    assert row["evidence"]["auth_hint"]


def test_nonzero_with_version_is_version_state(tmp_path: Path) -> None:
    script = tmp_path / "fake_ver.py"
    script.write_text("import sys; print('tool 2.1.0'); sys.exit(3)", encoding="utf-8")
    probes = write_probes(tmp_path, [{"id": "ver", "binaries": [sys.executable],
                                     "version_args": [str(script)],
                                     "ready_hints": ["\\d+\\.\\d+\\.\\d+"]}])
    row = detector.detect(probes)["results"][0]
    assert row["state"] == detector.STATE_VERSION
    assert row["version"] == "2.1.0"


def test_broken_binary_is_error_not_absent(tmp_path: Path) -> None:
    script = tmp_path / "fake_broken.py"
    script.write_text("import sys; sys.stderr.write('segfault-ish'); sys.exit(9)", encoding="utf-8")
    probes = write_probes(tmp_path, [{"id": "broken", "binaries": [sys.executable],
                                     "version_args": [str(script)]}])
    row = detector.detect(probes)["results"][0]
    assert row["state"] == detector.STATE_ERROR
    assert row["evidence"]["version_cmd"]["rc"] == 9


def test_only_filter_and_counts(tmp_path: Path) -> None:
    probes = write_probes(tmp_path, [
        {"id": "a", "binaries": ["nope-a"]},
        {"id": "b", "binaries": ["nope-b"]},
    ])
    report = detector.detect(probes, only=["a"])
    assert [r["id"] for r in report["results"]] == ["a"]
    assert report["counts"]["absent"] == 1


def test_shipped_probes_are_valid_json() -> None:
    data = detector.load_probes()
    ids = [a["id"] for a in data["agents"]]
    assert len(ids) >= 10
    assert len(set(ids)) == len(ids)
    for agent in data["agents"]:
        assert agent.get("binaries")
