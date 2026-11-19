"""P7 自检：席位记忆桥（A hub 代写 / B 派单注入 / C 契约门禁）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

import memory_bridge as MB  # noqa: E402
import memory_outbox as MO  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def block(**over):
    value = {"kind": "milestone", "subject": "席位记忆",
             "claim": "Codex 已按协议写入团队记忆",
             "evidence_level": "E1", "evidence": "uai-memory exit=0"}
    value.update(over)
    return MB.BLOCK_OPEN + json.dumps(value, ensure_ascii=False) + MB.BLOCK_CLOSE


def test_extract_plain():
    text = "结论如下。\n" + block() + "\n完。"
    blocks = MB.extract_blocks(text)
    check("MB-1 纯文本里能抽出块", len(blocks) == 1 and blocks[0]["kind"] == "milestone",
          str(blocks)[:120])
    check("MB-2 无块时返回空", MB.extract_blocks("没有块") == [], "")


def test_extract_jsonl():
    inner = block()
    line = json.dumps({"type": "item.completed",
                       "item": {"type": "agent_message", "text": "回报：" + inner}},
                      ensure_ascii=False)
    path = Path(tempfile.mkdtemp()) / "result.json"
    path.write_text(line + "\n", encoding="utf-8")
    text = MB.collect_result_text(path)
    blocks = MB.extract_blocks(text)
    check("MB-3 转义 JSONL 也能抽出块", len(blocks) == 1 and "claim" in blocks[0],
          str(len(blocks)))
    check("MB-4 抽出后内容未被转义污染",
          "Codex 已按协议" in blocks[0]["claim"], blocks[0].get("claim", "")[:40])


def test_invalid_block():
    text = MB.BLOCK_OPEN + "not json" + MB.BLOCK_CLOSE
    blocks = MB.extract_blocks(text)
    check("MB-5 坏块被标记而不是崩溃", bool(blocks) and "__error__" in blocks[0],
          str(blocks)[:80])


def test_capture():
    db = Path(tempfile.mkdtemp()) / "capture.db"
    result = MB.capture_from_text("x" + block() + "y", task_id="t-1", agent="codex",
                                  db_path=db)
    check("MB-6 有效块被代写入队", len(result["captured"]) == 1, str(result)[:120])
    ob = MO.MemoryOutbox(db)
    row = ob.get(result["captured"][0])
    check("MB-7 记忆归属席位与任务",
          row["task_id"] == "t-1" and "codex" in row["payload"], str(row["task_id"]))
    check("MB-8 幂等键可预测", result["captured"][0] == "seat:t-1:0",
          result["captured"][0])


def test_capture_rejects_credentials():
    db = Path(tempfile.mkdtemp()) / "bad.db"
    bad = block(claim="部署时使用 password: hunter2")
    result = MB.capture_from_text(bad, task_id="t-2", agent="codex", db_path=db)
    check("MB-9 含凭据的块被拒绝", result["captured"] == [], str(result)[:120])
    raw = b""
    for suffix in ("", "-wal"):
        path = Path(str(db) + suffix)
        if path.exists():
            raw += path.read_bytes()
    check("MB-10 凭据没有落盘", b"hunter2" not in raw, "bytes=%d" % len(raw))


def test_capture_from_pointer():
    directory = Path(tempfile.mkdtemp())
    result_file = directory / "result.json"
    result_file.write_text(json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": block()}}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    pointer = {"uri": result_file.as_uri(), "media_type": "application/json"}
    db = directory / "ptr.db"
    result = MB.capture_from_pointer(pointer, task_id="t-3", agent="tianshu", db_path=db)
    check("MB-11 能从结果指针代写", len(result["captured"]) == 1, str(result)[:120])
    missing = MB.capture_from_pointer({"uri": "memory://x"}, task_id="t-4", agent="x",
                                      db_path=db)
    check("MB-12 非文件指针不炸", missing["captured"] == [] and missing["rejected"],
          str(missing)[:80])


def test_contract_flag():
    yes = {"params": {"task_contract": {"memory_intent_required": True}}}
    no = {"params": {"task_contract": {"scope": "x"}}}
    empty = {}
    check("MB-13 契约要求记忆可识别", MB.contract_requires_memory(yes) is True, "")
    check("MB-14 未要求时为 False", MB.contract_requires_memory(no) is False, "")
    check("MB-15 无契约时为 False", MB.contract_requires_memory(empty) is False, "")


def test_recall_failopen():
    out = MB.recall_block("UAI", graph_url="http://127.0.0.1:59999", timeout=0.5)
    check("MB-16 图谱不可用时返回空串而不是抛错", out == "", repr(out))
    check("MB-17 空查询直接返回", MB.recall_block("") == "", "")


def main() -> int:
    test_extract_plain()
    test_extract_jsonl()
    test_invalid_block()
    test_capture()
    test_capture_rejects_credentials()
    test_capture_from_pointer()
    test_contract_flag()
    test_recall_failopen()
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
