"""订阅轮换留痕层（2026-11-15）。

设计参照 Paseo Accounts/Account Switcher（市面已验证）：
- 多订阅账号注册（provider、标识、限额口径）
- 限流事件检测（agent 执行返回 rate-limit/429/limit 文案）
- 切换决策：当前账号限流 → 选下一个可用账号（轮转）
- 全部事件（limit_hit/switch/usage）写 UAI 账本留痕——出站方向可追责

单账号时轮换退化为"限流事件留痕"（如实记录，不假装有第二账号）。
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

LIMIT_PATTERNS = [
    re.compile(r"rate.?limit", re.I),
    re.compile(r"usage limit", re.I),
    re.compile(r"429"),
    re.compile(r"quota exceeded", re.I),
    re.compile(r"limit reached", re.I),
    re.compile(r"too many requests", re.I),
]


def looks_rate_limited(text: str) -> bool:
    return any(p.search(text or "") for p in LIMIT_PATTERNS)


class SubscriptionRegistry:
    """账号注册表。文件形状：{"accounts":[{"id","provider","note"}], "active": "<id>"}"""

    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps({"accounts": [], "active": None}, ensure_ascii=False), encoding="utf-8")
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def add(self, account_id: str, provider: str, note: str = "") -> dict:
        if any(a["id"] == account_id for a in self.data["accounts"]):
            return {"added": False, "reason": "already_registered"}
        self.data["accounts"].append({"id": account_id, "provider": provider,
                                      "note": note, "registered_at": time.time()})
        if not self.data.get("active"):
            self.data["active"] = account_id
        self._save()
        return {"added": True, "active": self.data["active"]}

    def active(self) -> dict | None:
        aid = self.data.get("active")
        return next((a for a in self.data["accounts"] if a["id"] == aid), None)

    def rotate(self) -> dict | None:
        """限流后切到下一个账号（轮转）。单账号返回 None（无可切）。"""
        accs = self.data["accounts"]
        if len(accs) < 2:
            return None
        aid = self.data.get("active")
        idx = next((i for i, a in enumerate(accs) if a["id"] == aid), -1)
        nxt = accs[(idx + 1) % len(accs)]
        self.data["active"] = nxt["id"]
        self._save()
        return nxt

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


class RotationLedger:
    """轮换事件 → UAI 账本留痕。事件形状与 ledger.jsonl 记录兼容。"""

    def __init__(self, ledger_path: Path):
        self.ledger_path = Path(ledger_path)

    def record(self, record_type: str, **fields: Any) -> dict:
        value = {"record": record_type, "ts": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), **fields}
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        return value


class SubscriptionRotator:
    """组合：检测限流 → 决策切换 → 账本留痕。"""

    def __init__(self, registry_path: Path, ledger_path: Path):
        self.registry = SubscriptionRegistry(registry_path)
        self.ledger = RotationLedger(ledger_path)

    def on_agent_result(self, agent_id: str, reply: str, task_id: str | None = None) -> dict:
        """agent 执行结束后调用：限流检测与处理。"""
        if not looks_rate_limited(reply):
            return {"limited": False}
        self.ledger.record("SUB_LIMIT_HIT", task_id=task_id, agent_id=agent_id,
                           snippet=(reply or "")[:200])
        nxt = self.registry.rotate()
        if nxt:
            self.ledger.record("SUB_SWITCHED", agent_id=agent_id,
                               to_account=nxt["id"], provider=nxt["provider"])
            return {"limited": True, "switched_to": nxt["id"]}
        self.ledger.record("SUB_EXHAUSTED", agent_id=agent_id,
                           detail="no_alternative_account")
        return {"limited": True, "switched_to": None}
