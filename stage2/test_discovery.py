"""E-DISC: A2A 双向发现测试。"""
import json, os, sys, threading, time, tempfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from discovery import DiscoveryRegistry, AgentCardCache, fetch_remote_card, discover_all, build_dynamic_card, DiscoveryError

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    # 1. Registry 注册/去重/卸载
    work = Path(tempfile.mkdtemp())
    reg = DiscoveryRegistry(work / "peers.json")
    r1 = reg.register("https://hub-a.example.com", "Hub A")
    check("DISC-1 register", r1 == {"added": True, "url": "https://hub-a.example.com"}, str(r1))
    r2 = reg.register("https://hub-a.example.com")
    check("DISC-2 重复注册拒绝", r2 == {"added": False, "reason": "already_registered"}, str(r2))
    r3 = reg.unregister("https://hub-a.example.com")
    check("DISC-3 unregister", r3 == {"removed": 1}, str(r3))

    # 2. 非 http URL 拒绝
    try:
        reg.register("ftp://bad")
        check("DISC-4 非 http URL 拒绝", False, "竟然接受")
    except DiscoveryError:
        check("DISC-4 非 http URL 拒绝", True)

    # 3. 远端 AgentCard 拉取(起一个 mock HTTP server)
    mock_card = {"name": "remote-hub", "description": "test", "skills": []}
    class MockHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/.well-known/agent-card.json":
                body = json.dumps(mock_card).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()
        def log_message(self, fmt, *args): pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    card = fetch_remote_card("http://127.0.0.1:%d" % port)
    check("DISC-5 远端 AgentCard 拉取", card.get("name") == "remote-hub", str(card.get("name")))

    # 4. build_dynamic_card
    dc = build_dynamic_card("dsh", [{"name": "exec", "description": "run code"}], "http://10.0.0.1:8642", "http://10.0.0.1:43321")
    check("DISC-6 dynamic card 有 name", dc.get("name") == "dsh", "")
    check("DISC-7 dynamic card 有 uai 标注", dc.get("uai/acceptance_gate") is True, "")
    check("DISC-8 dynamic card 有 skills", len(dc.get("skills", [])) == 1, "")

    srv.shutdown()
    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
