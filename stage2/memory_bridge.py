"""席位记忆桥：hub 代写（A）+ 派单注入（B）+ 契约门禁（C）。

为什么需要它：席位跑在只读沙箱里（codex/rivet --sandbox read-only），
写不了共享 outbox；而模型也不会因为 AGENTS.md 写了就主动 recall。
所以记忆必须由中枢代写、由中枢注入，不能靠席位自觉。
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

BLOCK_OPEN = "[[MEMORY_INTENT]]"
BLOCK_CLOSE = "[[/MEMORY_INTENT]]"

MAX_RESULT_BYTES = 512 * 1024
MAX_BLOCKS = 5
MAX_RECALL_FACTS = 3
MAX_RECALL_CHARS = 400

DEFAULT_GRAPH_URL = os.environ.get("UAI_GRAPH_URL", "http://127.0.0.1:8642")

_BLOCK_RE = re.compile(re.escape(BLOCK_OPEN) + r"(.*?)" + re.escape(BLOCK_CLOSE), re.S)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _iter_strings(value: Any, depth: int = 0) -> Iterator[str]:
    if depth > 8:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item, depth + 1)


def collect_result_text(path: Path) -> str:
    """把结果文件变成纯文本：JSONL 取所有字符串叶子，纯文本原样。

    不能直接在原始 JSONL 上正则匹配块：内层引号被转义成 \\"，
    json.loads 之后才能拿到可解析的块内容。
    """
    try:
        raw = Path(path).read_bytes()[:MAX_RESULT_BYTES]
    except OSError:
        return ""
    text = raw.decode("utf-8", errors="replace")
    stripped = text.lstrip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return text
    parts: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            parts.append(line)
            continue
        parts.extend(_iter_strings(value))
    return "\n".join(parts)


def extract_blocks(text: str) -> list[dict[str, Any]]:
    """从结果文本里抽出全部记忆意图块；解析失败的块显式标记。"""
    found: list[dict[str, Any]] = []
    for match in _BLOCK_RE.finditer(text or ""):
        payload = match.group(1).strip()
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as exc:
            found.append({"__error__": "invalid_json:%s" % str(exc)[:80]})
            continue
        if isinstance(value, dict):
            found.append(value)
        else:
            found.append({"__error__": "block_not_object"})
        if len(found) >= MAX_BLOCKS:
            break
    return found


def build_intent(
    block: dict[str, Any], *, task_id: str, agent: str, index: int
) -> dict[str, Any]:
    intent = {k: v for k, v in block.items() if not k.startswith("__")}
    intent.setdefault("intent_id", "seat:%s:%d" % (task_id, index))
    intent["task_id"] = intent.get("task_id") or task_id
    intent["source_agent"] = agent
    intent.setdefault("group_id", "team")
    intent.setdefault("sensitivity", "internal")
    intent.setdefault("reference_time", _now_iso())
    intent.setdefault("evidence_level", "E3")
    return intent


def capture_from_text(
    text: str, *, task_id: str, agent: str, db_path: Any = None
) -> dict[str, Any]:
    """把结果里的块代写进 outbox（只入队，投递交给后台 drain）。"""
    blocks = extract_blocks(text)
    captured: list[str] = []
    rejected: list[dict[str, Any]] = []
    if not blocks:
        return {"captured": captured, "rejected": rejected, "found": 0}

    from memory_outbox import MemoryOutbox  # 延迟导入，缺失也不炸调用方

    outbox = MemoryOutbox(db_path)
    for index, block in enumerate(blocks):
        if "__error__" in block:
            rejected.append({"reason": block["__error__"]})
            continue
        intent = build_intent(block, task_id=task_id, agent=agent, index=index)
        result = outbox.enqueue(intent)
        if result.get("enqueued"):
            captured.append(intent["intent_id"])
        else:
            rejected.append({"reason": result.get("reason"),
                             "intent_id": intent.get("intent_id")})
    return {"captured": captured, "rejected": rejected, "found": len(blocks)}


def capture_from_pointer(
    pointer: dict[str, Any], *, task_id: str, agent: str, db_path: Any = None
) -> dict[str, Any]:
    uri = (pointer or {}).get("uri") or ""
    if not uri.startswith("file:"):
        return {"captured": [], "rejected": [{"reason": "no_file_pointer"}], "found": 0}
    from core import file_uri_to_path

    try:
        path = file_uri_to_path(uri)
    except Exception as exc:
        return {"captured": [], "rejected": [{"reason": "bad_pointer:%s" % str(exc)[:60]}], "found": 0}
    text = collect_result_text(path)
    return capture_from_text(text, task_id=task_id, agent=agent, db_path=db_path)


def contract_requires_memory(envelope: dict[str, Any]) -> bool:
    contract = ((envelope or {}).get("params") or {}).get("task_contract") or {}
    return bool(contract.get("memory_intent_required"))


def recall_block(query: str, *, limit: int = MAX_RECALL_FACTS, timeout: float = 2.0,
                 graph_url: str | None = None) -> str:
    """按任务描述取团队记忆，编译成单行可注入块。失败一律返回空串。"""
    text = (query or "").strip()
    if not text:
        return ""
    payload = json.dumps({"query": text[:400], "group_ids": ["team"],
                          "num_results": int(limit)}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        (graph_url or DEFAULT_GRAPH_URL).rstrip("/") + "/search",
        data=payload, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except Exception:
        return ""
    facts = [str(item.get("fact") or "").strip() for item in (body.get("facts") or [])]
    facts = [fact for fact in facts if fact][:limit]
    if not facts:
        return ""
    joined = "；".join("-%s" % fact for fact in facts)[:MAX_RECALL_CHARS]
    return " 【团队记忆·自动注入】" + joined + " 【注入结束】"
