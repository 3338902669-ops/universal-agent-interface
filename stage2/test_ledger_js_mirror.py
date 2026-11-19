"""E-JS: 账本 JetStream 镜像与回放测试。

需要 4223 中台启用 JetStream（2026-11-17 起 nats-hub.conf 已开）。
用独立 stream 名，避免污染 UAI_LEDGER。
"""
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import ledger_js_mirror as m  # noqa: E402

NATS = "nats://127.0.0.1:4223"
STREAM = "UAI_LEDGER_TEST"
PREFIX = "uai.ledgertest."
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def make_ledger(tmp, n):
    p = Path(tmp) / "ledger.jsonl"
    lines = []
    for i in range(n):
        rec = {"record": "TASK_STATE" if i % 2 else "VERDICT",
               "ts": "2026-11-17T00:00:%02dZ" % (i % 60),
               "seq": i, "self_hash": "h%03d" % i}
        lines.append(json.dumps(rec, ensure_ascii=False))
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def main():
    # ---- 纯函数部分 ----
    check("JS-1 subject sanitizes odd chars",
          m._subject_for("TASK-STATE x", PREFIX) == PREFIX + "TASK_STATE_x",
          m._subject_for("TASK-STATE x", PREFIX))
    check("JS-2 subject falls back to UNKNOWN",
          m._subject_for("", PREFIX) == PREFIX + "UNKNOWN", m._subject_for("", PREFIX))

    tmp = tempfile.mkdtemp()
    led = make_ledger(tmp, 6)
    recs = m.read_ledger(led)
    check("JS-3 read_ledger parses", len(recs) == 6, str(len(recs)))

    empty = Path(tmp) / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    check("JS-4 missing/empty ledger tolerated",
          m.read_ledger(Path(tmp) / "nope.jsonl") == [] and m.read_ledger(empty) == [], "")

    # ---- 需要 JetStream ----
    async def js_part():
        # 前置：删掉上次运行留下的测试流，避免重复累积影响计数断言
        import nats as _nats
        _nc = await _nats.connect(NATS, connect_timeout=5)
        try:
            try:
                await _nc.jetstream().delete_stream(STREAM)
            except Exception:
                pass
        finally:
            await _nc.close()

        created, info = await m.ensure_stream(NATS, stream=STREAM, subject_prefix=PREFIX)
        check("JS-5 stream created", created is True and info.get("name") == STREAM, str(info)[:80])

        created2, _ = await m.ensure_stream(NATS, stream=STREAM, subject_prefix=PREFIX)
        check("JS-6 ensure_stream idempotent", created2 is False, str(created2))

        pub, fail = await m.mirror_file(NATS, led, stream=STREAM, subject_prefix=PREFIX)
        check("JS-7 mirror all records", pub == 6 and fail == 0, "pub=%d fail=%d" % (pub, fail))

        v = await m.verify_against_file(NATS, led, stream=STREAM, subject_prefix=PREFIX)
        check("JS-8 verify ok after mirror", v["ok"] is True, json.dumps(v, ensure_ascii=False)[:110])
        check("JS-9 counts match and no duplicates",
              v["file_records"] == 6 and v["stream_records"] == 6
              and v["duplicates_in_stream"] == 0,
              "file=%d stream=%d dup=%d" % (v["file_records"], v["stream_records"],
                                           v["duplicates_in_stream"]))

        # 增量：state 不存在时首跑等同全量，推 6；再跑应推 0
        state = Path(tmp) / "js-state.json"
        r1a = await m.mirror_incremental(NATS, led, state, stream=STREAM, subject_prefix=PREFIX)
        check("JS-10a first incremental bootstraps from zero",
              r1a["pushed"] == 6 and r1a["offset_before"] == 0, str(r1a))
        r1b = await m.mirror_incremental(NATS, led, state, stream=STREAM, subject_prefix=PREFIX)
        check("JS-10b second incremental is a no-op",
              r1b["pushed"] == 0 and r1b["offset_after"] == 6, str(r1b))

        # 追加 2 条 -> 只推 2
        with led.open("a", encoding="utf-8") as f:
            for i in range(6, 8):
                f.write(json.dumps({"record": "TASK_EVENT", "seq": i,
                                    "self_hash": "h%03d" % i}, ensure_ascii=False) + "\n")
        r2 = await m.mirror_incremental(NATS, led, state, stream=STREAM, subject_prefix=PREFIX)
        check("JS-11 incremental pushes only new rows", r2["pushed"] == 2, str(r2))

        # 再校验：集合必须一致；重复推送要如实报 duplicates，但不影响 ok
        v2 = await m.verify_against_file(NATS, led, stream=STREAM, subject_prefix=PREFIX)
        check("JS-12 verify ok after incremental (dupes reported)",
              v2["ok"] and v2["unique_stream_records"] == 8 and v2["duplicates_in_stream"] > 0,
              json.dumps(v2, ensure_ascii=False)[:130])

    asyncio.run(js_part())

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
