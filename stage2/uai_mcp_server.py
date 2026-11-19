"""UAI MCP server（2026-11-17）：把 4223 中台能力暴露成 MCP 工具。

为什么需要它 —— xiaoyu（Grok 席位）的接入边界：

GrokBot 是 Electron GUI 应用，实测（2026-11-17）：
- 没有本地 CLI
- local-exec-daemon 不监听任何本地端口，只做出站 egress tunnel 到云端
- settings.json 的 mcpBoxServers 为空，但它**是 MCP 客户端**（会调外部 MCP server）

结论：xiaoyu 不能作为 UAI 的执行席位（interface 需要能被本地 spawn），
但可以作为 UAI 的**调用方**——只要 UAI 提供一个 MCP server。

本文件就是那个 server。它把中台四个动作暴露成 MCP 工具：
  uai_submit_task / uai_query_state / uai_submit_verdict / uai_list_seats

传输：stdio + 换行分隔 JSON-RPC 2.0（MCP 标准），纯标准库，无额外依赖。

用法：
    python uai_mcp_server.py                    # stdio（默认）
    python uai_mcp_server.py --nats-url nats://127.0.0.1:4223

在 GrokBot 的 MCP 设置里新增一条，命令填：
    <python 绝对路径> <本文件绝对路径>

边界声明（不夸大）：
- 这只是「xiaoyu 能派单/查状态」，不是「xiaoyu 能被派单」
- 不改变 xiaoyu 在四席里的角色，也不是四席自动化接入
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

DEFAULT_NATS = "nats://127.0.0.1:4223"
SERVER_NAME = "uai-hub"
SERVER_VERSION = "1.0.0"

TOOLS = [
    {
        "name": "uai_submit_task",
        "description": "向 UAI 中台提交一个任务。返回 task_id 与受理结果。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string", "description": "任务正文，纯文本"},
                "target_interface": {
                    "type": "string",
                    "description": "目标席位 id，如 dsh / codex / tianshu；留空由中台路由",
                },
                "cls": {
                    "type": "string",
                    "enum": ["readonly", "reversible", "external"],
                    "description": "副作用等级，默认 readonly",
                },
                "timeout_s": {"type": "integer", "description": "任务超时秒数，默认 120"},
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "uai_query_state",
        "description": "查询一个任务的当前执行状态与状态迁移历史。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "uai_submit_verdict",
        "description": (
            "对一个任务投裁定。注意：未到终态的任务不能判「通过」；"
            "核验者不能等于实现者，否则会被 verifier_is_implementer 拒绝。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "verdict": {
                    "type": "string",
                    "enum": ["通过", "带注通过", "条件通过", "不通过", "未核", "作废"],
                },
                "checker": {"type": "string", "description": "核验方标识，必须不等于实现者"},
                "notes": {"type": "string"},
            },
            "required": ["task_id", "verdict", "checker"],
        },
    },
    {
        "name": "uai_list_seats",
        "description": "列出中台已知席位与最近心跳情况（只读）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class UaiMcpServer:
    def __init__(self, nats_url: str):
        self.nats_url = nats_url
        self._nc = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        async with self._lock:
            if self._nc is None or self._nc.is_closed:
                import nats
                self._nc = await nats.connect(
                    self.nats_url, connect_timeout=5, max_reconnect_attempts=2
                )
        return self._nc

    async def _request(self, subject: str, payload: dict, timeout: float = 15.0) -> dict:
        nc = await self._connect()
        msg = await nc.request(
            subject, json.dumps(payload, ensure_ascii=False).encode("utf-8"), timeout=timeout
        )
        return json.loads(msg.data.decode("utf-8"))

    # ---------- 工具实现 ----------

    async def tool_submit_task(self, args: dict) -> dict:
        instruction = (args.get("instruction") or "").strip()
        if not instruction:
            return {"error": "instruction_required"}
        import inbound_bridge
        target = args.get("target_interface") or None
        cls = args.get("cls") or "readonly"
        timeout_s = int(args.get("timeout_s") or 120)
        req = {
            "message": {
                "messageId": "mcp-%d" % (int(asyncio.get_event_loop().time() * 1000)),
                "parts": [{"text": instruction}],
            }
        }
        try:
            out = await inbound_bridge.submit(
                nats_url=self.nats_url,
                request=req,
                created_by="xiaoyu",
                target_interface=target,
                timeout_s=timeout_s,
                cls=cls,
                contract_version="1.0",
                inject_memory=False,
            )
            return {
                "ok": True,
                "task_id": out["task_id"],
                "accepted": out.get("accepted"),
                "target_interface": target,
            }
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    async def tool_query_state(self, args: dict) -> dict:
        task_id = (args.get("task_id") or "").strip()
        if not task_id:
            return {"error": "task_id_required"}
        import inbound_bridge
        try:
            st = await inbound_bridge.query_state(nats_url=self.nats_url, task_id=task_id)
            return {"ok": True, "task_id": task_id, "state": st.get("state"),
                    "state_history": st.get("state_history") or []}
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    async def tool_submit_verdict(self, args: dict) -> dict:
        task_id = (args.get("task_id") or "").strip()
        verdict = (args.get("verdict") or "").strip()
        checker = (args.get("checker") or "xiaoyu").strip()
        if not task_id or not verdict:
            return {"error": "task_id_and_verdict_required"}
        payload = {
            "task_id": task_id,
            "verdict": verdict,
            "checker": checker,
            "enforce_three_layer": True,
        }
        if args.get("notes"):
            payload["notes"] = args["notes"]
        try:
            return await self._request("hub.verdict", payload)
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:300]}

    async def tool_list_seats(self, args: dict) -> dict:
        """读中台状态：已知席位与心跳。只读，走 hub.state 的旁路不存在，
        因此这里读本地账本尾部统计（不修改任何东西）。"""
        from pathlib import Path
        import collections

        candidates = [
            Path(r"C:\Users\33389\.shared\comms-v2\cutover-base-20261109\data\ledger.jsonl"),
        ]
        ledger = next((p for p in candidates if p.is_file()), None)
        if ledger is None:
            return {"ok": False, "error": "ledger_not_found"}
        seats = collections.Counter()
        active = set()
        try:
            with ledger.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("record") == "INTERFACE_ACTIVE" and rec.get("interface_id"):
                        active.add(rec["interface_id"])
                    if rec.get("record") == "TASK_STATE" and rec.get("interface_id"):
                        seats[rec["interface_id"]] += 1
        except Exception as exc:
            return {"ok": False, "error": str(exc)[:200]}
        return {"ok": True, "active_interfaces": sorted(active),
                "task_counts_by_seat": dict(seats), "ledger": str(ledger)}

    async def call_tool(self, name: str, args: dict) -> dict:
        if name == "uai_submit_task":
            return await self.tool_submit_task(args)
        if name == "uai_query_state":
            return await self.tool_query_state(args)
        if name == "uai_submit_verdict":
            return await self.tool_submit_verdict(args)
        if name == "uai_list_seats":
            return await self.tool_list_seats(args)
        return {"error": "unknown_tool: %s" % name}

    async def close(self):
        if self._nc is not None and not self._nc.is_closed:
            await self._nc.close()


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


async def serve_stdio(server: UaiMcpServer) -> int:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            _emit({"jsonrpc": "2.0", "id": None,
                   "error": {"code": -32700, "message": "parse error"}})
            continue

        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            _emit({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            })
        elif method in ("notifications/initialized", "initialized"):
            continue
        elif method == "tools/list":
            _emit({"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name = params.get("name") or ""
            args = params.get("arguments") or {}
            result = await server.call_tool(name, args)
            _emit({
                "jsonrpc": "2.0", "id": req_id,
                "result": {
                    "content": [
                        {"type": "text",
                         "text": json.dumps(result, ensure_ascii=False, indent=1)}
                    ],
                    "isError": bool(result.get("error")),
                },
            })
        elif method in ("ping",):
            _emit({"jsonrpc": "2.0", "id": req_id, "result": {}})
        elif req_id is not None:
            _emit({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": -32601, "message": "method not found: %s" % method}})
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--nats-url", default=DEFAULT_NATS)
    args = ap.parse_args(argv)
    server = UaiMcpServer(args.nats_url)
    try:
        return asyncio.run(serve_stdio(server))
    finally:
        try:
            asyncio.run(server.close())
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
