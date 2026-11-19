"""Push Notification 投递器（2026-11-15，方案 A，规格 protocol/PUSH-NOTIFICATION-SPEC.md）。

独立于 event_bridge v1/v2：旁路读账本增量，只推终态相关事件到注册的回调 URL。
不阻塞主链路（P-3）：投递失败重试 3 次后死信落盘。

用法：
    import asyncio, push_notifier
    asyncio.run(push_notifier.run_once(base, callback_table))  # 单次扫描
    # 或持续模式
    asyncio.run(push_notifier.run_forever(base, callback_table, interval_s=2))
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

GENESIS = "0" * 64
TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}
BACKOFF_S = (2, 8, 32)


def load_push_configs(path: Path) -> dict[str, dict]:
    """callback_table: task_id -> {"url", "token"?}。由受理侧写入（见 spec §2）。"""
    if not path.exists():
        return {}
    out = {}
    for line in io.open(path, encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = r.get("task_id")
        url = r.get("url")
        if tid and url:
            out[tid] = {"url": url, "token": r.get("token")}
    return out


def scan_terminal_events(ledger_path: Path, start_offset: int) -> tuple[list[dict], int]:
    """从 start_offset 起读账本，返回 (新终态事件列表, 新 offset)。

    事件形状：{"task_id", "seq"(单调递增，按该任务事件序), "kind"(state|verdict),
              "state"/"verdict", "ts"}
    """
    events: list[dict] = []
    if not Path(ledger_path).exists():
        return events, start_offset
    size = Path(ledger_path).stat().st_size
    if size <= start_offset:
        return events, start_offset
    seqs: dict[str, int] = {}
    with Path(ledger_path).open("r", encoding="utf-8", errors="replace") as fh:
        fh.seek(start_offset)
        blob = fh.read()
    # 起始行可能撕裂：丢弃第一行残段（直到第一个换行）
    if start_offset > 0 and blob and not blob.startswith("{"):
        blob = blob[blob.find("\n") + 1:]
    for line in blob.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        tid = r.get("task_id")
        if not tid:
            continue
        seqs[tid] = seqs.get(tid, 0) + 1
        if r.get("record") == "TASK_STATE" and r.get("state") in TERMINAL_STATES:
            events.append({"task_id": tid, "seq": seqs[tid], "kind": "state",
                           "state": r["state"], "ts": r.get("ts")})
        elif r.get("record") == "VERDICT":
            events.append({"task_id": tid, "seq": seqs[tid], "kind": "verdict",
                           "verdict": r.get("verdict"), "ts": r.get("ts")})
    return events, size


def sign_body(token: str, timestamp: str, body: bytes) -> str:
    msg = (timestamp + "." + body.decode("utf-8", "replace")).encode("utf-8")
    return hmac.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def deliver(url: str, event: dict, token: str | None, timeout_s: float = 10) -> tuple[bool, str]:
    """单次投递。返回 (ok, detail)。"""
    body = json.dumps(event, ensure_ascii=False).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "X-UAI-Task-Id": event["task_id"],
               "X-UAI-Seq": str(event["seq"]),
               "X-UAI-Timestamp": str(int(time.time()))}
    if token:  # P-4：有 token 必签
        headers["X-UAI-Signature"] = sign_body(token, headers["X-UAI-Timestamp"], body)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return 200 <= resp.status < 300, "http_%d" % resp.status
    except urllib.error.HTTPError as e:
        return False, "http_%d" % e.code
    except Exception as e:
        return False, str(e)[:80]


async def run_once(base: Path, callback_table_path: Path,
                   state_path: Path | None = None) -> dict:
    """扫描一次账本增量，投递终态事件。返回统计。"""
    base = Path(base)
    ledger = base / "data" / "ledger.jsonl"
    state_path = state_path or (base / "data" / "push-offset.json")
    dead = base / "data" / "push-dead-letters.jsonl"
    plog = base / "data" / "push-log.jsonl"

    offset = GENESIS and 0
    if state_path.exists():
        try:
            offset = json.loads(state_path.read_text(encoding="utf-8")).get("offset", 0)
        except Exception:
            offset = 0
    configs = load_push_configs(callback_table_path)
    events, new_offset = scan_terminal_events(ledger, offset)

    delivered = failed = deadlettered = 0
    for ev in events:
        cfg = configs.get(ev["task_id"])
        if not cfg:
            continue  # 未注册回调的任务零回调（验收 B）
        ok, detail = False, ""
        for attempt, wait in enumerate((0,) + BACKOFF_S):
            if wait:
                await asyncio.sleep(wait)
            ok, detail = deliver(cfg["url"], ev, cfg.get("token"))
            _log(plog, ev, cfg["url"], detail, attempt + 1)
            if ok:
                break
        if ok:
            delivered += 1
        else:
            failed += 1
            _deadletter(dead, ev, cfg, detail)  # P-2：死信不丢
            deadlettered += 1

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"offset": new_offset}, separators=(",", ":")), encoding="utf-8")
    return {"events": len(events), "delivered": delivered,
            "failed": failed, "deadlettered": deadlettered, "offset": new_offset}


def _log(plog: Path, ev: dict, url: str, detail: str, attempt: int) -> None:
    plog.parent.mkdir(parents=True, exist_ok=True)
    with plog.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": time.time(), "task_id": ev["task_id"], "seq": ev["seq"],
                             "url": url, "result": detail, "attempt": attempt},
                            ensure_ascii=False, separators=(",", ":")) + "\n")


def _deadletter(dead: Path, ev: dict, cfg: dict, detail: str) -> None:
    dead.parent.mkdir(parents=True, exist_ok=True)
    with dead.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"ts": time.time(), "event": ev, "url": cfg["url"],
                             "last_error": detail}, ensure_ascii=False,
                            separators=(",", ":")) + "\n")


async def run_forever(base: Path, callback_table_path: Path, interval_s: float = 2) -> None:
    while True:
        await run_once(base, callback_table_path)
        await asyncio.sleep(interval_s)
