"""UAI 记忆审计：回答「哪些有结论但没入图」。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}


def _ledger_rows(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    rows.append(value)
    except OSError:
        return []
    return rows


def build_audit(
    outbox: Any,
    *,
    ledger_path: str | Path | None = None,
) -> dict[str, Any]:
    terminal: dict[str, str] = {}
    if ledger_path:
        for row in _ledger_rows(ledger_path):
            if row.get("record") != "TASK_STATE":
                continue
            state = row.get("state")
            task_id = row.get("task_id")
            if state in TERMINAL_STATES and task_id:
                terminal[task_id] = state

    # 只认「delivered + 回读 verified」：写了但没确认的不算有记忆
    verified = outbox.verified_task_ids()
    delivered = outbox.delivered_task_ids()
    any_intent = outbox.task_ids()
    unrecorded = []
    for task_id, state in terminal.items():
        if task_id in verified:
            continue
        if task_id in delivered:
            reason = "unverified_write"
        elif task_id in any_intent:
            reason = "intent_not_delivered"
        else:
            reason = "no_memory_intent"
        unrecorded.append({"task_id": task_id, "state": state, "reason": reason})
    with_memory = sum(1 for task_id in terminal if task_id in verified)

    return {
        "memory": outbox.stats(),
        "degraded": outbox.degraded_rows(),
        "dead_letters": outbox.dead_letters(),
        "unrecorded": unrecorded,
        "unverified": outbox.unverified_rows(),
        "coverage": {"with_memory": with_memory, "terminal": len(terminal)},
    }


def render_text(audit: dict[str, Any]) -> str:
    memory = audit["memory"]
    coverage = audit["coverage"]
    lines = [
        "团队记忆审计",
        "=" * 40,
        "总数 %d | 已写 %d | 待发 %d | 重试 %d | 死信 %d"
        % (
            memory.get("total", 0),
            memory.get("delivered", 0),
            memory.get("pending", 0) + memory.get("inflight", 0),
            memory.get("retry", 0),
            memory.get("dead_letter", 0),
        ),
        "覆盖 %d/%d 任务有记忆"
        % (coverage["with_memory"], coverage["terminal"]),
        "",
        "有结论未入图：%d" % len(audit["unrecorded"]),
    ]
    for row in audit["unrecorded"]:
        lines.append("  - %s (%s)" % (row["task_id"], row["state"]))
    lines.append("")
    lines.append("写入未确认（不算有记忆）：%d" % len(audit.get("unverified", [])))
    for row in audit.get("unverified", []):
        lines.append("  - %s path=%s" % (row["intent_id"], row.get("write_path")))
    lines.append("")
    lines.append("降级写入（不可语义检索，需补抽取）：%d" % len(audit["degraded"]))
    for row in audit["degraded"]:
        lines.append("  - %s path=%s" % (row["intent_id"], row.get("write_path")))
    lines.append("")
    lines.append("死信：%d" % len(audit["dead_letters"]))
    for row in audit["dead_letters"]:
        lines.append(
            "  - %s attempts=%s error=%s"
            % (row["intent_id"], row.get("attempts"), (row.get("last_error") or "")[:80])
        )
    return "\n".join(lines)
