"""E-HB: 心跳必须与主账本分离。

背景：心跳是连接保活，1.8s 一轮 × 3 席 ≈ 100 行/分钟，
一度把账本冲到 3819 行而业务记录只有几十条，主账本几乎不可用；
且 _replay 根本不读 INTERFACE_HEARTBEAT，这些记录纯属噪声。

断言：
1. on_heartbeat 写 heartbeats.jsonl，不写 ledger.jsonl
2. 主账本仍保留状态变更（INTERFACE_ACTIVE 等）
3. 心跳记录带 ts 与 interface_id
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

from hub import Hub  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def main():
    base = Path(tempfile.mkdtemp())
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    hub = Hub(base, "nats://127.0.0.1:4223")

    ledger = base / "data" / "ledger.jsonl"
    hb = base / "data" / "heartbeats.jsonl"

    check("HB-1 heartbeat file wired", hasattr(hub, "heartbeat_log"), "")

    # 连发 5 次心跳
    for _ in range(5):
        hub.record_heartbeat("dsh")

    # 同时写一条状态变更（应进主账本）
    hub.record("INTERFACE_ACTIVE", interface_id="dsh")

    hb_lines = [json.loads(l) for l in hb.read_text(encoding="utf-8").splitlines() if l.strip()] if hb.exists() else []
    lg_lines = [json.loads(l) for l in ledger.read_text(encoding="utf-8").splitlines() if l.strip()] if ledger.exists() else []

    check("HB-2 heartbeats written to separate file", len(hb_lines) == 5, str(len(hb_lines)))
    check("HB-3 ledger has no heartbeat records",
          all(r.get("record") != "INTERFACE_HEARTBEAT" for r in lg_lines),
          str([r.get("record") for r in lg_lines]))
    check("HB-4 state change still in ledger",
          any(r.get("record") == "INTERFACE_ACTIVE" for r in lg_lines), "")
    check("HB-5 heartbeat entry shape",
          hb_lines and "ts" in hb_lines[0] and hb_lines[0].get("interface_id") == "dsh",
          str(hb_lines[0] if hb_lines else None)[:80])
    check("HB-6 heartbeat not hash-chained",
          hb_lines and "self_hash" not in hb_lines[0],
          str(sorted(hb_lines[0].keys()) if hb_lines else None))
    check("HB-7 ledger entry is hash-chained",
          lg_lines and "self_hash" in lg_lines[-1],
          str(sorted(lg_lines[-1].keys()) if lg_lines else None))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())