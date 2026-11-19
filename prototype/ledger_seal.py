"""账本哈希链接线（2026-11-15）：让 hub 写出的每条记录可被篡改检出。

设计：
- 启动时扫描 ledger.jsonl 尾部，取最后一条带 self_hash 的记录作为链头；
  历史未链化记录不算失败（渐进启用，与 stage2/ledger_chain.py 的口径一致）。
- record() 出口统一 seal：self_hash = sha256(prev_hash + canonical(record 去掉 self_hash))。
- 每 50 条把当前 chain head 追加到 data/chain-heads.jsonl 作为外部锚点。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
ANCHOR_EVERY = 50


def canonical(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k not in ("self_hash", "prev_hash")}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def compute_self_hash(record: dict[str, Any], prev_hash: str) -> str:
    return hashlib.sha256((prev_hash + canonical(record)).encode("utf-8")).hexdigest()


def tail_head(ledger_path: Path) -> str:
    """扫描账本尾部，返回最后一条链化记录的 self_hash；无则 GENESIS。"""
    head = GENESIS
    if not ledger_path.exists():
        return head
    with ledger_path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            h = r.get("self_hash")
            if isinstance(h, str) and len(h) == 64:
                head = h
    return head


class LedgerSealer:
    """给 AppendOnlyJsonl 写路径加 seal 的薄层。"""

    def __init__(self, ledger_path: Path, anchors_path: Path):
        self.ledger_path = Path(ledger_path)
        self.anchors_path = Path(anchors_path)
        self.head = tail_head(self.ledger_path)
        self.count = 0

    def seal(self, record: dict[str, Any]) -> dict[str, Any]:
        sealed = dict(record)
        sealed["prev_hash"] = self.head
        sealed["self_hash"] = compute_self_hash(record, self.head)
        self.head = sealed["self_hash"]
        self.count += 1
        if self.count % ANCHOR_EVERY == 0:
            self._anchor()
        return sealed

    def _anchor(self) -> None:
        self.anchors_path.parent.mkdir(parents=True, exist_ok=True)
        with self.anchors_path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps({"ts_anchor_count": self.count,
                                 "chain_head": self.head},
                                ensure_ascii=False, separators=(",", ":")) + "\n")
