"""S8-live:hub 写路径哈希链验证。

前置:真 NATS + hub;跑完至少产生若干新记录(用 hub.debug/reconcile 或直接提交任务)。
验证:1) 新记录全部带 self_hash/prev_hash;2) verify_chain 全链通过;3) 篡改一行可检出。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import urllib.request
from pathlib import Path

NATS_URL = os.environ.get("NATS_URL", "nats://127.0.0.1:43321")
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def canonical(record):
    body = {k: v for k, v in record.items() if k not in ("self_hash", "prev_hash")}
    return json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def main() -> int:
    base = Path(__file__).resolve().parent.parent / "prototype"
    ledger = base / "data" / "ledger.jsonl"

    # 1. 先经 hub 写至少 3 条新记录:直接 NATS hub.submit 太重,用 lab_client 提交一个 test executor 任务
    sys.path.insert(0, str(base))
    import asyncio
    import lab_client
    async def submit_one():
        c = lab_client.LabClient(NATS_URL)
        await c.connect()
        env = lab_client.build_envelope(instruction="chain test", capability="runtime_exec",
                                         cls="readonly", timeout_s=30)
        resp = await c.submit(env)
        await c.nc.close()
        return resp
    resp = asyncio.run(submit_one())
    check("S8-live hub 受信新任务", resp.get("ok") is True, str(resp)[:80])

    rows = [json.loads(l) for l in io.open(ledger, encoding="utf-8") if l.strip()]
    new_rows = [r for r in rows if r.get("self_hash")]
    check("S8-live 存在新链化记录", len(new_rows) >= 3, "count=%d" % len(new_rows))
    check("S8-live 新记录首条 prev=GENESIS 或接历史",
          all(isinstance(r.get("prev_hash"), str) and len(r["prev_hash"]) == 64 for r in new_rows), "")

    # 2. 全链重算:从第一条链化记录起,prev 接续且 self_hash 一致
    ok, broken = True, []
    prev = "0" * 64
    started = False
    for idx, r in enumerate(rows):
        if "self_hash" not in r:
            continue
        if not started:
            started = True  # 第一条链化记录允许 prev=GENESIS 或接到未链化尾部(本库历史尾部无 hash,即 GENESIS)
        expect = hashlib.sha256((r["prev_hash"] + canonical(r)).encode("utf-8")).hexdigest()
        if r["prev_hash"] != prev and not (prev == "0"*64 and r["prev_hash"] == "0"*64):
            ok = False; broken.append((idx, "prev_gap"))
        if r["self_hash"] != expect:
            ok = False; broken.append((idx, "self_hash_mismatch"))
        prev = r["self_hash"]
    check("S8-live 全链 verify 通过", ok, str(broken[:3]))

    # 3. 篡改检出:复制账本,改一条内容,重算失败
    import tempfile
    tmp = Path(tempfile.mkdtemp()) / "tampered.jsonl"
    tampered = [dict(r) for r in rows]
    for r in reversed(tampered):
        if "self_hash" in r:
            r["record"] = "TAMPERED"
            break
    with tmp.open("w", encoding="utf-8") as fh:
        for r in tampered:
            fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
    ok2, broken2 = True, []
    prev = "0" * 64
    for idx, r in enumerate(t for t in tampered if "self_hash" in t):
        expect = hashlib.sha256((r["prev_hash"] + canonical(r)).encode("utf-8")).hexdigest()
        if r["self_hash"] != expect:
            ok2 = False; broken2.append(idx)
    check("S8-live 篡改被检出", not ok2, str(broken2[:3]))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
