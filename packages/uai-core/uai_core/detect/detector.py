"""agent 三层检测：存在性 → 可用性 → 能力。

铁律：**没装 ≠ 认证失败 ≠ 版本不兼容**。三种状态必须分开，否则界面上的绿点就是假的。
检测只用一次带超时的短命令，不产生副作用；GUI 型 agent 用进程名判定「在跑」。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..oslayer import proc

DEFAULT_PROBES = Path(__file__).with_name("probes.json")

STATE_READY = "ready"
STATE_AUTH = "auth_failed"
STATE_VERSION = "version_incompatible"
STATE_ABSENT = "absent"
STATE_ERROR = "error"
STATE_RUNNING = "running"


def load_probes(path: str | os.PathLike | None = None) -> dict:
    p = Path(path) if path else DEFAULT_PROBES
    return json.loads(p.read_text(encoding="utf-8"))


def _extra_bin_dirs() -> list[Path]:
    """常见安装目录补充（which 找不到时的兜底）。"""
    home = Path.home()
    cands = [
        home / ".local" / "bin",
        home / "bin",
        home / "AppData" / "Roaming" / "npm",
        home / "AppData" / "Local" / "Programs",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        Path("/usr/bin"),
    ]
    return [p for p in cands if p.is_dir()]


def _which(binaries: list[str]) -> str | None:
    for name in binaries:
        found = shutil.which(name)
        if found:
            return found
    for base_dir in _extra_bin_dirs():
        for name in binaries:
            for suffix in ("", ".cmd", ".exe", ".ps1", ".bat"):
                cand = base_dir / (name + suffix)
                if cand.exists():
                    return str(cand)
    return None


def _run_version(binary: str, args: list[str], timeout_s: float) -> tuple[int, str]:
    cmd = [binary] + list(args)
    if binary.lower().endswith((".cmd", ".bat")) and os.name == "nt":
        cmd = ["cmd", "/c"] + cmd
    elif binary.lower().endswith(".ps1") and os.name == "nt":
        cmd = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"] + cmd
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s,
                             errors="replace")
        return res.returncode, ((res.stdout or "") + (res.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout after %.0fs" % timeout_s
    except FileNotFoundError:
        return 127, "not found"
    except Exception as exc:
        return 1, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def _match_any(patterns: list[str], text: str) -> str | None:
    lowered = (text or "").lower()
    for pat in patterns or []:
        try:
            if re.search(pat, lowered, re.IGNORECASE):
                return pat
        except re.error:
            if str(pat).lower() in lowered:
                return pat
    return None


def _http_probe(url: str, timeout_s: float) -> tuple[bool, str]:
    import urllib.request

    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            body = resp.read(2048).decode("utf-8", "replace")
            return True, body
    except Exception as exc:
        return False, "%s: %s" % (type(exc).__name__, str(exc)[:120])


def detect_one(entry: dict, *, timeout_s: float = 8.0) -> dict:
    """检测一个 agent，返回带证据的结果。"""
    eid = entry.get("id")
    out = {
        "id": eid,
        "label": entry.get("label", eid),
        "vendor": entry.get("vendor"),
        "kind": entry.get("kind", "cli"),
        "state": STATE_ABSENT,
        "version": None,
        "binary": None,
        "capability_tags": list(entry.get("capability_tags") or []),
        "adapter": entry.get("adapter"),
        "evidence": {},
    }

    processes = entry.get("processes") or []
    if entry.get("kind") == "gui" and processes:
        rows = proc.find(name=processes)
        out["evidence"]["processes"] = [r["pid"] for r in rows]
        if rows:
            out["state"] = STATE_RUNNING
            out["version"] = None
            out["binary"] = rows[0].get("name")
            return out

    binary = _which(entry.get("binaries") or [])
    if not binary:
        http_probe = entry.get("http_probe")
        if http_probe:
            ok, body = _http_probe(http_probe, timeout_s)
            out["evidence"]["http_probe"] = {"url": http_probe, "ok": ok, "body_head": body[:200]}
            if ok:
                out["state"] = STATE_READY
                out["binary"] = http_probe
    if not binary:
        return out

    out["binary"] = binary
    args = entry.get("version_args") or ["--version"]
    rc, text = _run_version(binary, args, timeout_s)
    out["evidence"]["version_cmd"] = {"cmd": " ".join([binary] + args), "rc": rc, "text_head": text[:300]}

    version = None
    hints = entry.get("ready_hints") or []
    for pat in hints:
        m = re.search(pat, text)
        if m:
            version = m.group(0)
            break
    out["version"] = version

    if rc == 0:
        out["state"] = STATE_READY
        return out

    auth_hit = _match_any(entry.get("auth_fail_patterns") or [], text)
    if auth_hit:
        out["state"] = STATE_AUTH
        out["evidence"]["auth_hint"] = auth_hit
        return out

    if version:
        out["state"] = STATE_VERSION
        out["evidence"]["note"] = "binary responds but exits non-zero"
        return out

    out["state"] = STATE_ERROR
    return out


def detect(path: str | os.PathLike | None = None, *, only: list[str] | None = None,
           timeout_s: float = 8.0) -> dict:
    probes = load_probes(path)
    entries = probes.get("agents") or []
    if only:
        wanted = {str(x).lower() for x in only}
        entries = [e for e in entries if str(e.get("id")).lower() in wanted]
    results = [detect_one(e, timeout_s=timeout_s) for e in entries]
    counts: dict[str, int] = {}
    for row in results:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {
        "schema": probes.get("schema"),
        "platform": {"os": os.name, "sys": sys.platform, "python": sys.version.split()[0]},
        "counts": counts,
        "ready": [r["id"] for r in results if r["state"] in (STATE_READY, STATE_RUNNING)],
        "results": results,
    }
