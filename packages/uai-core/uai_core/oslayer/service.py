"""开机自启：三个平台各一个适配器，接口统一。

为什么要进核心：旧方案是 PowerShell + vbs 隐藏启动，只有 Windows 有，
而且**只有启动没有停止**，反复触发会叠出第二套（真实事故：2 中枢 + 8 席位）。
现在自启单元只做一件事：拉起 uai serve（Python 监督循环），幂等与清场都在循环里。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

UNIT_NAME = "uai-core"


def platform_kind() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "launchd"
    return "systemd"


def exec_command(extra: list[str] | None = None) -> list[str]:
    """自启单元实际执行的命令。"""
    python = sys.executable
    if platform_kind() == "windows":
        pythonw = Path(python).with_name("pythonw.exe")
        if pythonw.exists():
            python = str(pythonw)
    return [python, "-m", "uai_core", "serve"] + list(extra or [])


def _home() -> Path:
    return Path.home()


def _startup_dir() -> Path:
    return Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _systemd_dir() -> Path:
    return _home() / ".config" / "systemd" / "user"


def _autostart_dir() -> Path:
    return _home() / ".config" / "autostart"


def _has_systemctl() -> bool:
    from shutil import which
    return which("systemctl") is not None


def _render_systemd(cmd: list[str]) -> str:
    lines = [
        "[Unit]",
        "Description=UAI collaboration core (supervisor)",
        "",
        "[Service]",
        "Type=simple",
        "ExecStart=" + " ".join(cmd),
        "Restart=always",
        "RestartSec=5",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return "\n".join(lines)


def _render_desktop(cmd: list[str]) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=UAI Core",
        "Exec=" + " ".join(cmd),
        "X-GNOME-Autostart-enabled=true",
        "Terminal=false",
        "",
    ]
    return "\n".join(lines)


def _render_vbs(cmd: list[str]) -> str:
    """Windows 登录自启：用 Chr(34) 拼引号，避免嵌套转义地狱。"""
    exe = cmd[0]
    rest = " ".join(cmd[1:])
    return (
        "Set sh = CreateObject(\"WScript.Shell\")" + "\r\n"
        + "sh.Run Chr(34) & \"" + exe + "\" & Chr(34) & \" " + rest + "\", 0, False" + "\r\n"
    )


def _plist(cmd: list[str]) -> str:
    args = "".join("    <string>" + c + "</string>\n" for c in cmd)
    return "\n".join([
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">',
        '<plist version="1.0"><dict>',
        "  <key>Label</key><string>com.uai.core</string>",
        "  <key>ProgramArguments</key><array>",
        args.rstrip("\n"),
        "  </array>",
        "  <key>RunAtLoad</key><true/>",
        "  <key>KeepAlive</key><true/>",
        "</dict></plist>",
        "",
    ])


def install(extra: list[str] | None = None) -> dict:
    """注册用户级自启（不动系统级配置）。"""
    kind = platform_kind()
    cmd = exec_command(extra)
    if kind == "windows":
        target_dir = _startup_dir()
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / "uai-core.vbs"
        target.write_text(_render_vbs(cmd), encoding="utf-8")
        return {"platform": kind, "installed": str(target), "command": cmd}
    if kind == "launchd":
        agents = _home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        target = agents / "com.uai.core.plist"
        target.write_text(_plist(cmd), encoding="utf-8")
        try:
            subprocess.run(["launchctl", "load", str(target)], capture_output=True, text=True, timeout=30)
        except Exception:
            pass
        return {"platform": kind, "installed": str(target), "command": cmd}
    if _has_systemctl():
        unit_dir = _systemd_dir()
        unit_dir.mkdir(parents=True, exist_ok=True)
        target = unit_dir / (UNIT_NAME + ".service")
        target.write_text(_render_systemd(cmd), encoding="utf-8")
        for args in (["systemctl", "--user", "daemon-reload"],
                     ["systemctl", "--user", "enable", "--now", UNIT_NAME]):
            try:
                subprocess.run(args, capture_output=True, text=True, timeout=30)
            except Exception:
                pass
        return {"platform": kind, "installed": str(target), "command": cmd}
    entry_dir = _autostart_dir()
    entry_dir.mkdir(parents=True, exist_ok=True)
    target = entry_dir / (UNIT_NAME + ".desktop")
    target.write_text(_render_desktop(cmd), encoding="utf-8")
    return {"platform": kind, "installed": str(target), "command": cmd,
            "note": "no systemctl; fell back to XDG autostart"}


def _current_path() -> Path:
    kind = platform_kind()
    if kind == "windows":
        return _startup_dir() / "uai-core.vbs"
    if kind == "launchd":
        return _home() / "Library" / "LaunchAgents" / "com.uai.core.plist"
    unit = _systemd_dir() / (UNIT_NAME + ".service")
    if unit.exists():
        return unit
    return _autostart_dir() / (UNIT_NAME + ".desktop")


def uninstall() -> dict:
    kind = platform_kind()
    removed = []
    target = _current_path()
    if kind == "systemd" and _has_systemctl():
        try:
            subprocess.run(["systemctl", "--user", "disable", "--now", UNIT_NAME],
                           capture_output=True, text=True, timeout=30)
        except Exception:
            pass
    if kind == "launchd" and target.exists():
        try:
            subprocess.run(["launchctl", "unload", str(target)], capture_output=True, text=True, timeout=30)
        except Exception:
            pass
    if target.exists():
        target.unlink()
        removed.append(str(target))
    return {"platform": kind, "removed": removed}


def status() -> dict:
    target = _current_path()
    return {"platform": platform_kind(), "registered": target.exists(), "path": str(target)}
