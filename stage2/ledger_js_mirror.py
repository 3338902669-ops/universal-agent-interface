"""账本 → JetStream 镜像与回放（2026-11-17）。

## 为什么做这个

2026-10-23 路线 1 定版：NATS JetStream 作传输层。但规格 §11 要求的
「落盘发件箱 + seq 补投 + 补投完成前不接新单」已由 interface 自研实现，
所以 JetStream 不应替换那条路径（会破坏已通过的 19 个套件）。

它在这里的真实用途是：**账本的持久化副本与跨机回放**。
本地账本是 append-only JSONL，单机磁盘；JetStream 给同一批记录一份
可流式消费、可跨机重放的副本。出事时能回答「别的机器上还有没有这份记录」。

## 设计

- Stream 名 UAI_LEDGER，主题 uai.ledger.<record>（record = 记录类型）
- 镜像默认关闭：hub 侧由环境变量 UAI_JS_MIRROR=1 打开
- 每批记录都可按原样回放（保留 self_hash/prev_hash，可用于链校验）
- verify_against_file() 用 self_hash 集合比对镜像与本地账本

## 用法

    import asyncio, ledger_js_mirror as m
    asyncio.run(m.ensure_stream("nats://127.0.0.1:4223"))
    asyncio.run(m.mirror_file("nats://127.0.0.1:4223", "path/ledger.jsonl"))
    r = asyncio.run(m.verify_against_file("nats://127.0.0.1:4223", "path/ledger.jsonl"))
"""

from __future__ import annotations

import json
from pathlib import Path

STREAM = "UAI_LEDGER"
SUBJECT_PREFIX = "uai.ledger."
SUBJECTS = [SUBJECT_PREFIX + ">"]


async def _connect(nats_url):
    import nats
    return await nats.connect(nats_url, connect_timeout=5, max_reconnect_attempts=2)


async def ensure_stream(nats_url, stream=STREAM, subject_prefix=SUBJECT_PREFIX):
    """幂等创建 stream。返回 (created, config_summary)。"""
    nc = await _connect(nats_url)
    try:
        js = nc.jetstream()
        try:
            info = await js.stream_info(stream)
            return False, {
                "name": stream,
                "messages": info.state.messages,
                "bytes": info.state.bytes,
                "subjects": list(info.config.subjects or []),
            }
        except Exception:
            pass
        cfg = {"name": stream, "subjects": [subject_prefix + ">"], "storage": "file"}
        info = await js.add_stream(**cfg)
        return True, {
            "name": stream,
            "messages": info.state.messages,
            "bytes": info.state.bytes,
            "subjects": list(info.config.subjects or []),
        }
    finally:
        await nc.close()


def _subject_for(record_type, subject_prefix=SUBJECT_PREFIX):
    safe = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(record_type or "UNKNOWN"))
    return subject_prefix + (safe or "UNKNOWN")


async def mirror_records(nats_url, records, stream=STREAM, subject_prefix=SUBJECT_PREFIX):
    """把一组账本记录 publish 到 JetStream。返回 (published, failed)。"""
    if not records:
        return 0, 0
    nc = await _connect(nats_url)
    published = failed = 0
    try:
        js = nc.jetstream()
        try:
            await js.stream_info(stream)
        except Exception:
            await js.add_stream(name=stream, subjects=[subject_prefix + ">"], storage="file")
        for rec in records:
            subj = _subject_for(rec.get("record"), subject_prefix)
            try:
                await js.publish(subj, json.dumps(rec, ensure_ascii=False).encode("utf-8"))
                published += 1
            except Exception:
                failed += 1
    finally:
        await nc.close()
    return published, failed


def read_ledger(path):
    p = Path(path)
    if not p.is_file():
        return []
    out = []
    with p.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


async def mirror_file(nats_url, ledger_path, stream=STREAM, subject_prefix=SUBJECT_PREFIX):
    """把本地账本全量镜像进 JetStream。重跑会重复，日常用 mirror_incremental。"""
    records = read_ledger(ledger_path)
    return await mirror_records(nats_url, records, stream, subject_prefix)


async def mirror_incremental(nats_url, ledger_path, state_path, stream=STREAM,
                             subject_prefix=SUBJECT_PREFIX):
    """增量镜像：按账本行数水位只推新增。

    state 文件形如 {"offset": 5138}。返回 {pushed, total, offset_before, offset_after}。
    """
    state_p = Path(state_path)
    offset = 0
    if state_p.is_file():
        try:
            offset = int(json.loads(state_p.read_text(encoding="utf-8")).get("offset", 0))
        except Exception:
            offset = 0
    records = read_ledger(ledger_path)
    total = len(records)
    if offset > total:
        offset = total
    new_records = records[offset:]
    pub, fail = await mirror_records(nats_url, new_records, stream, subject_prefix)
    state_p.parent.mkdir(parents=True, exist_ok=True)
    state_p.write_text(json.dumps({"offset": total, "pushed": pub, "failed": fail}),
                       encoding="utf-8")
    return {"pushed": pub, "failed": fail, "total": total,
            "offset_before": offset, "offset_after": total}


async def replay_all(nats_url, limit=10000, timeout_s=10.0, stream=STREAM,
                     subject_prefix=SUBJECT_PREFIX):
    """读回镜像里的全部消息，返回记录列表（按流内顺序）。"""
    nc = await _connect(nats_url)
    out = []
    try:
        js = nc.jetstream()
        info = await js.stream_info(stream)
        total = info.state.messages
        if total == 0:
            return out
        sub = await js.subscribe(subject_prefix + ">", stream=stream,
                                 deliver_policy="all", ordered_consumer=True)
        try:
            want = min(total, limit)
            for _ in range(want):
                try:
                    msg = await sub.next_msg(timeout=timeout_s)
                except Exception:
                    break
                try:
                    out.append(json.loads(msg.data.decode("utf-8")))
                except Exception:
                    continue
        finally:
            await sub.unsubscribe()
    finally:
        await nc.close()
    return out


async def verify_against_file(nats_url, ledger_path, limit=20000, stream=STREAM,
                             subject_prefix=SUBJECT_PREFIX):
    """用 self_hash 集合比对镜像与本地账本。

    返回 {file_records, stream_records, missing_in_stream, extra_in_stream, ok}
    missing/extra 用 self_hash 判定；无 self_hash 的旧记录用 (record, ts) 兜底。
    """
    file_recs = read_ledger(ledger_path)
    stream_recs = await replay_all(nats_url, limit=limit, stream=stream,
                                   subject_prefix=subject_prefix)

    def key(r):
        h = r.get("self_hash")
        if h:
            return "h:" + str(h)
        return "t:%s|%s" % (r.get("record"), r.get("ts"))

    file_keys = [key(r) for r in file_recs]
    stream_keys = [key(r) for r in stream_recs]
    file_set, stream_set = set(file_keys), set(stream_keys)

    missing = [k for k in file_keys if k not in stream_set]
    extra = [k for k in stream_keys if k not in file_set]
    # 同一记录被重复推送（例如先全量再增量）不算 extra，但要显式报出来
    duplicates = len(stream_keys) - len(stream_set)
    return {
        "file_records": len(file_recs),
        "stream_records": len(stream_recs),
        "unique_stream_records": len(stream_set),
        "duplicates_in_stream": duplicates,
        "missing_in_stream": len(missing),
        "extra_in_stream": len(extra),
        "ok": (not missing) and (not extra),
        "sample_missing": missing[:3],
        "sample_extra": extra[:3],
    }


def _main(argv=None) -> int:
    """CLI：python ledger_js_mirror.py --nats-url ... --ledger ... [--state ...] [--verify]"""
    import argparse, asyncio, sys
    ap = argparse.ArgumentParser()
    ap.add_argument("--nats-url", default="nats://127.0.0.1:4223")
    ap.add_argument("--ledger", required=True)
    ap.add_argument("--stream", default=STREAM)
    ap.add_argument("--state", default=None, help="增量水位文件；给了就增量，不给就全量")
    ap.add_argument("--verify", action="store_true", help="只校验，不写")
    a = ap.parse_args(argv)

    async def run():
        if a.verify:
            return await verify_against_file(a.nats_url, a.ledger, stream=a.stream)
        await ensure_stream(a.nats_url, stream=a.stream)
        if a.state:
            return await mirror_incremental(a.nats_url, a.ledger, a.state, stream=a.stream)
        pub, fail = await mirror_file(a.nats_url, a.ledger, stream=a.stream)
        return {"mirrored": pub, "failed": fail}

    print(json.dumps(asyncio.run(run()), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
