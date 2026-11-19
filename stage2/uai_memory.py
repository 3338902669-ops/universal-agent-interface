#!/usr/bin/env python3
"""uai-memory：四席位统一的团队记忆入口。

    uai-memory remember --kind decision --subject "..." --claim "..." --evidence "cmd exit=0"
    uai-memory recall "UAI 记忆管道现在什么状态"
    uai-memory audit
    uai-memory flush
    uai-memory replay --intent-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import memory_audit as audit_mod  # noqa: E402
import memory_pipeline as pipeline  # noqa: E402
import memory_writer as writer  # noqa: E402
from memory_outbox import MemoryOutbox  # noqa: E402


def _print(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_remember(args: argparse.Namespace) -> int:
    intent = pipeline.build_intent(
        kind=args.kind,
        claim=args.claim,
        subject=args.subject,
        evidence_level=args.evidence_level,
        evidence=args.evidence or "",
        task_id=args.task_id,
        group_id=args.group,
        agent=args.agent,
        session=args.session,
        intent_id=args.intent_id,
        refs=dict(item.split("=", 1) for item in (args.ref or []) if "=" in item),
    )
    result = pipeline.remember(intent, db_path=args.db, flush=not args.no_flush)
    result["intent_id"] = intent["intent_id"]
    _print(result)
    return 0 if result.get("enqueued") else 2


def cmd_recall(args: argparse.Namespace) -> int:
    payload = json.dumps(
        {"query": args.query, "group_ids": [args.group], "num_results": args.limit},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        args.graph_url.rstrip("/") + "/search",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=args.timeout) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
    except Exception as exc:
        print("recall 失败：%s" % exc, file=sys.stderr)
        return 1
    for fact in body.get("facts", []):
        print("- %s" % fact.get("fact"))
    if not body.get("facts"):
        print("（无匹配事实）")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    outbox = MemoryOutbox(args.db)
    ledger = args.ledger
    if ledger and not Path(ledger).exists():
        print("警告：账本不存在 %s" % ledger, file=sys.stderr)
        ledger = None
    report = audit_mod.build_audit(outbox, ledger_path=ledger)
    if args.json:
        _print(report)
    else:
        print(audit_mod.render_text(report))
    return 0


def cmd_flush(args: argparse.Namespace) -> int:
    outbox = MemoryOutbox(args.db)
    result = writer.flush_until_empty(outbox, max_rounds=args.rounds, limit=args.limit)
    _print(result)
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    outbox = MemoryOutbox(args.db)
    ok = outbox.replay(args.intent_id)
    _print({"replayed": ok, "intent_id": args.intent_id})
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="uai-memory", description="UAI 团队记忆入口")
    parser.add_argument("--db", default=None, help="outbox 数据库路径（默认 ~/.shared/graph-memory/outbox.db）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("remember", help="写入一条结构化记忆")
    p.add_argument("--kind", required=True,
                   choices=["decision", "milestone", "failure", "blockage", "handoff", "verdict"])
    p.add_argument("--subject", required=True)
    p.add_argument("--claim", required=True)
    p.add_argument("--evidence-level", default="E3", choices=["E1", "E2", "E3", "E4"])
    p.add_argument("--evidence", default="")
    p.add_argument("--task-id", default=None)
    p.add_argument("--group", default="team")
    p.add_argument("--agent", default=None)
    p.add_argument("--session", default="")
    p.add_argument("--intent-id", default=None)
    p.add_argument("--ref", action="append", default=[], metavar="KEY=VALUE")
    p.add_argument("--no-flush", action="store_true", help="只入队，不立即投递")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("recall", help="语义检索团队记忆")
    p.add_argument("query")
    p.add_argument("--group", default="team")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--graph-url", default=writer.DEFAULT_GRAPH_URL)
    p.add_argument("--timeout", type=float, default=15.0)
    p.set_defaults(func=cmd_recall)

    p = sub.add_parser("audit", help="审计：有结论未入图 / 降级 / 死信 / 覆盖率")
    p.add_argument("--ledger", default=pipeline.DEFAULT_LEDGER)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("flush", help="把 outbox 里的待发记忆全部投递")
    p.add_argument("--rounds", type=int, default=20)
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_flush)

    p = sub.add_parser("replay", help="重放一条死信")
    p.add_argument("--intent-id", required=True)
    p.set_defaults(func=cmd_replay)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
