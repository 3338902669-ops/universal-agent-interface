"""E-MCP: UAI MCP server 契约测试（stdio JSON-RPC）。

不去猜 GrokBot 的 MCP 配置 schema（mcpBoxServers 为空，盲改会弄坏 GUI），
先保证我们这一侧的 server 严格符合 MCP 线协议：
initialize → tools/list → tools/call，错误码正确，未知方法拒绝。

真实派单默认跳过（需要 4223 中台在线）；设 UAI_MCP_E2E=1 才跑。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "uai_mcp_server.py"
PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


class McpClient:
    """最小 MCP stdio 客户端：换行分隔 JSON-RPC。"""

    def __init__(self, nats_url="nats://127.0.0.1:4223"):
        self.proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--nats-url", nats_url],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        self._id = 0

    def send(self, method, params=None, notify=False):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if not notify:
            self._id += 1
            msg["id"] = self._id
        self.proc.stdin.write(json.dumps(msg, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        return json.loads(line)

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.kill()


def main():
    c = McpClient()

    # 1. initialize
    init = c.send("initialize", {"protocolVersion": "2024-11-05",
                                 "clientInfo": {"name": "test", "version": "0"}})
    res = init.get("result") or {}
    check("MCP-1 initialize returns serverInfo",
          (res.get("serverInfo") or {}).get("name") == "uai-hub", str(res.get("serverInfo")))
    check("MCP-2 initialize declares tools capability",
          "tools" in (res.get("capabilities") or {}), str(res.get("capabilities")))

    # 2. notifications/initialized（不应有响应，只确认不崩）
    c.send("notifications/initialized", None, notify=True)

    # 3. tools/list
    tl = c.send("tools/list", {})
    tools = (tl.get("result") or {}).get("tools") or []
    names = [t.get("name") for t in tools]
    check("MCP-3 tools/list count", len(tools) == 4, str(len(tools)))
    check("MCP-4 expected tool names",
          names == ["uai_submit_task", "uai_query_state", "uai_submit_verdict", "uai_list_seats"],
          str(names))
    check("MCP-5 every tool has inputSchema",
          all(isinstance(t.get("inputSchema"), dict) for t in tools), "")

    # 4. tools/call 只读工具 uai_list_seats
    ls = c.send("tools/call", {"name": "uai_list_seats", "arguments": {}})
    content = (ls.get("result") or {}).get("content") or []
    ok_payload = {}
    if content and content[0].get("type") == "text":
        try:
            ok_payload = json.loads(content[0]["text"])
        except Exception:
            pass
    check("MCP-6 list_seats returns payload", isinstance(ok_payload, dict) and "ok" in ok_payload,
          str(ok_payload)[:80])
    check("MCP-7 list_seats reports ledger",
          bool(ok_payload.get("ledger")), str(ok_payload.get("ledger"))[:70])
    check("MCP-8 isError false on success", (ls.get("result") or {}).get("isError") is False,
          str((ls.get("result") or {}).get("isError")))

    # 5. 参数缺失应报错而不崩
    bad = c.send("tools/call", {"name": "uai_query_state", "arguments": {}})
    bad_payload = {}
    bc = (bad.get("result") or {}).get("content") or []
    if bc:
        try:
            bad_payload = json.loads(bc[0]["text"])
        except Exception:
            pass
    check("MCP-9 missing task_id -> error payload",
          "task_id_required" in str(bad_payload), str(bad_payload)[:70])
    check("MCP-10 isError true on failure", (bad.get("result") or {}).get("isError") is True,
          str((bad.get("result") or {}).get("isError")))

    # 6. 未知方法 -> -32601
    unk = c.send("resources/list", {})
    check("MCP-11 unknown method -> -32601",
          (unk.get("error") or {}).get("code") == -32601, str(unk.get("error"))[:80])

    # 7. 畸形 JSON -> -32700
    c.proc.stdin.write("not json\n")
    c.proc.stdin.flush()
    pe = json.loads(c.proc.stdout.readline())
    check("MCP-12 malformed input -> -32700",
          (pe.get("error") or {}).get("code") == -32700, str(pe.get("error"))[:80])

    # 8. ping
    pg = c.send("ping", {})
    check("MCP-13 ping ok", "result" in pg, str(pg)[:60])

    # 9. 真实派单（可选）
    if os.environ.get("UAI_MCP_E2E") == "1":
        sub = c.send("tools/call", {
            "name": "uai_submit_task",
            "arguments": {"instruction": "Reply with exactly: MCP_E2E_OK",
                          "target_interface": "tianshu", "timeout_s": 180},
        })
        sj = {}
        sc = (sub.get("result") or {}).get("content") or []
        if sc:
            try:
                sj = json.loads(sc[0]["text"])
            except Exception:
                pass
        check("MCP-14 submit returns task_id", bool(sj.get("task_id")), str(sj)[:100])
    else:
        print("SKIP  MCP-14 真实派单（设 UAI_MCP_E2E=1 才跑）")

    c.close()
    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
