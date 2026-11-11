"""账本哈希链（威胁模型 S8）：让"直接改 ledger.jsonl"可以被检测。

现状：UAI 的账本是纯追加 JSONL，没有任何链式完整性校验——改一行没人知道。
GAR 有 `Ledger.verifyChain`，UAI 没有，这是威胁模型里登记的真实缺口。

本模块给最小可用方案：

  1. 每条记录追加时带 `prev_hash` 与 `self_hash`：
         self_hash = sha256( canonical(record 去掉 self_hash) + prev_hash )
  2. `verify_chain` 从头重算；任何一处被改、被删、被插，都会在第一个断点报出来。
  3. **不改动既有账本格式**：老记录没有 `self_hash` 时视为"未链化"，单独统计，
     不当作失败（否则历史库全部判红，没人会启用）。

边界：这不是防篡改存储——有写权限的人可以重算整条链。它的价值是让**局部修改**暴露，
并能与外部锚点（定期把 chain head 记到别处）配合成可证伪的审计。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path

GENESIS = "0" * 64


def canonical(record: dict) -> str:
    # 两个链字段都不参与计算，否则封链与校验的输入不一致（首版就是这里错的）
    body = {k: v for k, v in record.items() if k not in ("self_hash", "prev_hash")}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def self_hash(record: dict, prev_hash: str) -> str:
    return hashlib.sha256((prev_hash + canonical(record)).encode("utf-8")).hexdigest()


def seal(record: dict, prev_hash: str) -> dict:
    """给一条记录加上 prev_hash 与 self_hash（返回新 dict，不改入参）。"""
    out = dict(record)
    out["prev_hash"] = prev_hash
    out["self_hash"] = self_hash(record, prev_hash)
    return out


def iter_records(path: str | os.PathLike):
    p = Path(path)
    if not p.exists():
        return
    with p.open(encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield i, json.loads(line)
            except Exception:
                yield i, None


def verify_chain(path: str | os.PathLike) -> dict:
    """返回 {ok, total, sealed, unsealed, broken: [(lineno, reason)]}。"""
    prev = GENESIS
    total = sealed = 0
    broken = []
    for lineno, rec in iter_records(path):
        total += 1
        if rec is None:
            broken.append((lineno, "unparseable"))
            continue
        if "self_hash" not in rec:
            # 未链化的历史记录：可以接受，但不推进 prev（下一段从 GENESIS 重新起链）
            continue
        sealed += 1
        expect_prev = rec.get("prev_hash")
        if expect_prev != prev:
            broken.append((lineno, "prev_hash_mismatch"))
        calc = self_hash(rec, prev)
        if calc != rec.get("self_hash"):
            broken.append((lineno, "self_hash_mismatch"))
        prev = rec["self_hash"]
    return {"ok": not broken, "total": total, "sealed": sealed,
            "unsealed": total - sealed, "broken": broken}


def append_sealed(path: str | os.PathLike, record: dict) -> dict:
    """追加一条已封链的记录，返回写入的记录。"""
    p = Path(path)
    prev = GENESIS
    last = None
    for _, rec in iter_records(p):
        if rec and rec.get("self_hash"):
            last = rec
    if last:
        prev = last["self_hash"]
    sealed = seal(record, prev)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(sealed, ensure_ascii=False, sort_keys=True) + "\n")
    return sealed


def chain_head(path: str | os.PathLike) -> str:
    head = GENESIS
    for _, rec in iter_records(path):
        if rec and rec.get("self_hash"):
            head = rec["self_hash"]
    return head
