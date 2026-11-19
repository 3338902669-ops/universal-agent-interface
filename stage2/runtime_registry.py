"""多厂商 runtime 注册与一键接入（2026-11-16）。

解决：新 agent 怎么零成本加入协作系统。
现在只需要一个 YAML 文件声明所有 runtime，一条命令全部启动。

配置文件格式 (runtime_registry.yaml 或 .json)：
    runtimes:
      - id: codex
        command: codex
        type: native_cli          # 直接是 CLI 二进制
        description: OpenAI Codex
      - id: claude
        command: claude
        type: native_cli
        description: Anthropic Claude Code
      - id: dsh
        command: python stage2/runtime_adapters/dsh_adapter.py
        type: python_adapter       # 需要 python 解释器包装的 .py 脚本
        description: DeepSeek Harness
      - id: custom
        command: /path/to/my/agent
        type: native_cli
        max_concurrent: 3

用法：
    from runtime_registry import RuntimeRegistry
    reg = RuntimeRegistry.load("runtime_registry.yaml")
    reg.validate()   # 检查每个 CLI 是否存在
    reg.provision_all(hub_base)  # 创建 tokens
    # 启动（每个 runtime 一个 interface.py 进程）
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class RegistryError(ValueError):
    pass


class RuntimeEntry:
    """一个 runtime 的完整配置。"""

    def __init__(self, runtime_id: str, command: str, runtime_type: str = "native_cli",
                 description: str = "", max_concurrent: int = 2,
                 args: list[str] | None = None, env: dict[str, str] | None = None):
        self.runtime_id = runtime_id
        self.command = command
        self.runtime_type = runtime_type  # native_cli | python_adapter
        self.description = description
        self.max_concurrent = max_concurrent
        self.args = args or []
        self.env = env or {}

    def validate(self) -> dict:
        """检查这个 runtime 的 CLI 是否存在/可执行。"""
        cmd_parts = self.command.split()
        exe = cmd_parts[0]
        if os.path.isfile(exe) and os.access(exe, os.X_OK):
            return {"ok": True, "path": exe}
        found = shutil.which(exe)
        if found:
            return {"ok": True, "path": found}
        return {"ok": False, "error": "not_found: %s" % exe}

    def to_dict(self) -> dict:
        return {
            "id": self.runtime_id,
            "command": self.command,
            "type": self.runtime_type,
            "description": self.description,
            "max_concurrent": self.max_concurrent,
        }


class RuntimeRegistry:
    """多厂商 runtime 注册表：从配置文件加载、验证、批量启动。"""

    def __init__(self, entries: list[RuntimeEntry] | None = None):
        self.entries = entries or []

    @classmethod
    def load(cls, config_path: str | Path) -> "RuntimeRegistry":
        """从 YAML/JSON 配置文件加载 runtime 列表。"""
        path = Path(config_path)
        if not path.exists():
            raise RegistryError("config_not_found: %s" % path)
        text = path.read_text(encoding="utf-8")
        if path.suffix in (".yaml", ".yml"):
            try:
                import yaml
                data = yaml.safe_load(text)
            except ImportError:
                raise RegistryError("pyyaml required for yaml config: pip install pyyaml")
        else:
            data = json.loads(text)
        runtimes = data.get("runtimes", [])
        entries = []
        for r in runtimes:
            if not r.get("id") or not r.get("command"):
                continue  # 跳过不完整的条目
            entries.append(RuntimeEntry(
                runtime_id=r["id"], command=r["command"],
                runtime_type=r.get("type", "native_cli"),
                description=r.get("description", ""),
                max_concurrent=r.get("max_concurrent", 2),
                args=r.get("args"),
                env=r.get("env"),
            ))
        return cls(entries)

    def validate_all(self) -> list[dict]:
        """验证所有 runtime。返回逐个结果。"""
        results = []
        for entry in self.entries:
            v = entry.validate()
            results.append({"id": entry.runtime_id, "ok": v["ok"],
                           "detail": v.get("path") or v.get("error", "")})
        return results

    def provision_all(self, hub_base: str | Path) -> dict:
        """为所有 runtime 创建 token 和注册到 hub。"""
        base = Path(hub_base)
        token_dir = base / ".lab-tokens"
        token_dir.mkdir(parents=True, exist_ok=True)
        catalog_path = base / "data" / "token-hashes.json"
        catalog = json.loads(catalog_path.read_text(encoding="utf-8")) if catalog_path.exists() else {}
        import hashlib, secrets
        created = []
        for entry in self.entries:
            token_path = token_dir / ("%s.token" % entry.runtime_id)
            if not token_path.exists():
                token = secrets.token_urlsafe(32)
                token_path.write_text(token + "\n", encoding="utf-8")
                catalog[entry.runtime_id] = hashlib.sha256(token.encode()).hexdigest()
                created.append(entry.runtime_id)
        # 写 catalog
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=1), encoding="utf-8")
        return {"created": created, "catalog": catalog_path}

    def launch_commands(self, hub_base: str | Path, nats_url: str) -> list[dict]:
        """生成每个 runtime 的启动命令（不执行，只返回）。"""
        base = Path(hub_base)
        commands = []
        for entry in self.entries:
            iface_py = base / "interface.py"
            cmd = [
                "python", str(iface_py),
                "--base", str(base),
                "--interface-id", entry.runtime_id,
                "--token-file", str(base / ".lab-tokens" / ("%s.token" % entry.runtime_id)),
                "--nats-url", nats_url,
                "--executor", "runtime",
                "--runtime-executable", entry.command,
                "--max-concurrency", str(entry.max_concurrent),
            ]
            commands.append({"id": entry.runtime_id, "cmd": " ".join(cmd)})
        return commands
