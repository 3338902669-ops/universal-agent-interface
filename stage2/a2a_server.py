"""Stage 2 A2A 只读投影服务。

设计原则（映射表 v1.4 §7.1）：
  - 内部账本 `data/ledger.jsonl` 是唯一事实源；本服务**只读**，从不写账本。
  - 投影幂等：同一账本快照必得同一响应。
  - 服务挂掉不影响控制面。

端点：
  GET /.well-known/agent-card.json   → A2A AgentCard（来自 --card 指定的 JSON）
  GET /tasks                          → Task 列表（仅 id 与 state）
  GET /tasks/<task_id>                → A2A Task（含投影后的 status 与裁定）

裁定读取：账本里历史值 `驳回` 在**读出时**归一为 `不通过`（写路径另由 projection.assert_writable_verdict 拒绝）。
"""

from __future__ import annotations

import argparse
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import projection as P


def load_ledger(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def build_view(records: list[dict]) -> dict:
    """账本 → {task_id: {"state":..., "verdict":..., "ts":...}}；后写覆盖先写。"""
    view: dict[str, dict] = {}
    for rec in records:
        kind = rec.get("record")
        tid = rec.get("task_id")
        if not tid:
            continue
        slot = view.setdefault(tid, {})
        if kind == "TASK_STATE" and rec.get("state"):
            slot["state"] = rec["state"]
            slot["ts"] = rec.get("ts")
        elif kind == "VERDICT" and rec.get("verdict"):
            # 读路径归一：历史 驳回 → 不通过
            slot["verdict"] = P.canonicalize_verdict(str(rec["verdict"]))
            slot["verdict_ts"] = rec.get("ts")
            slot["checker"] = rec.get("checker")
    return view


def project_task_from_view(task_id: str, slot: dict) -> dict:
    state = slot.get("state")
    if not state:
        raise P.ProjectionError("no_state_for_task: %s" % task_id)
    task = P.project_task(
        task_id=task_id,
        internal_state=state,
        verdict=slot.get("verdict"),
        timestamp=slot.get("ts"),
    )
    if slot.get("checker"):
        task["metadata"] = {"uai/checker": slot["checker"]}
    return task


class Handler(BaseHTTPRequestHandler):
    server_version = "uai-a2a-projection/1.0"
    ledger_path: Path = Path("data/ledger.jsonl")
    card: dict | None = None

    def log_message(self, fmt, *args):  # 静音
        return

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"

        if path == "/.well-known/agent-card.json":
            if not self.card:
                return self._send(503, {"error": "card_not_configured"})
            return self._send(200, self.card)

        try:
            view = build_view(load_ledger(self.ledger_path))
        except Exception as e:  # 投影失败不得影响控制面
            return self._send(500, {"error": "ledger_unreadable", "detail": str(e)})

        if path == "/tasks":
            return self._send(
                200,
                {
                    "tasks": [
                        {"id": tid, "state": P.state_of(slot["state"]) if slot.get("state") else None}
                        for tid, slot in sorted(view.items())
                    ]
                },
            )

        if path.startswith("/tasks/"):
            tid = path[len("/tasks/"):]
            slot = view.get(tid)
            if not slot:
                return self._send(404, {"error": "task_not_found", "task_id": tid})
            try:
                return self._send(200, project_task_from_view(tid, slot))
            except P.ProjectionError as e:
                return self._send(422, {"error": "projection_failed", "detail": str(e)})

        return self._send(404, {"error": "not_found", "path": path})


def make_server(base: Path, card: dict | None, port: int = 0) -> ThreadingHTTPServer:
    handler = type(
        "BoundHandler",
        (Handler,),
        {"ledger_path": Path(base) / "data" / "ledger.jsonl", "card": card},
    )
    return ThreadingHTTPServer(("127.0.0.1", port), handler)


def serve_in_thread(base: Path, card: dict | None, port: int = 0):
    srv = make_server(base, card, port)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=".")
    ap.add_argument("--port", type=int, default=43801)
    ap.add_argument("--card", default=None, help="AgentCard JSON 文件")
    args = ap.parse_args()

    base = Path(args.base).resolve()
    card = json.loads(Path(args.card).read_text(encoding="utf-8")) if args.card else None
    srv = make_server(base, card, args.port)
    print(
        json.dumps(
            {
                "event": "a2a_projection_ready",
                "pid": os.getpid(),
                "listen": "127.0.0.1:%d" % srv.server_address[1],
                "ledger": str(base / "data" / "ledger.jsonl"),
                "card_configured": bool(card),
            },
            ensure_ascii=False,
        )
    )
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
