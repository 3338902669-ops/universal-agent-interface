"""E-OIDC-LIVE:真实 IdP 接入端到端。

1) 本机起 OIDC 规范 IdP(discovery + jwks_uri,真 RS256 签发)→ OidcVerifier(jwks_fetch=True)
   走完整 discovery 流程验签升 authenticated;
2) 真实公网 IdP(Microsoft 公共 JWKS 端点)拉取验证——证明能接真实厂商(无真凭据,只验证拉取与 key 解析)。
"""
from __future__ import annotations

import base64
import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from identity import OidcVerifier, ASSERTED, AUTHENTICATED

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main() -> int:
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes

    # ---- 本机 OIDC IdP(discovery + jwks_uri) ----
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()
    n_b = nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")
    e_b = nums.e.to_bytes(3, "big")
    jwks_doc = {"keys": [{"kty": "RSA", "kid": "live-1", "n": b64u(n_b), "e": b64u(e_b),
                           "use": "sig", "alg": "RS256"}]}

    class IdpHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == "/.well-known/openid-configuration":
                body = json.dumps({
                    "issuer": ISSUER,
                    "jwks_uri": ISSUER + "/jwks.json",
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/jwks.json":
                body = json.dumps(jwks_doc).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404); self.end_headers()

        def log_message(self, fmt, *args):  # 静音
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), IdpHandler)
    port = srv.server_address[1]
    ISSUER = "http://127.0.0.1:%d" % port
    # 注意:_fetch_jwks 拒绝回环 issuer(SSRF 防护)——本机 IdP 测试需旁路。
    # 处理:SSRF 拦截写在 _fetch_jwks 里,测试时用 127.0.0.1 会被拒。
    # → 这本身就是一条待验证的安全行为!先验证拦截,再用直接注入 jwks 的方式测拉取逻辑,
    #   或者给 OidcVerifier 加 allow_loopback 测试开关。
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    now = int(time.time())
    header = {"alg": "RS256", "kid": "live-1", "typ": "JWT"}
    payload = {"iss": ISSUER, "sub": "live-user-1", "aud": "uai-hub",
               "exp": now + 600, "iat": now}
    si = b64u(json.dumps(header).encode()) + "." + b64u(json.dumps(payload).encode())
    sig = key.sign(si.encode(), padding.PKCS1v15(), hashes.SHA256())
    token = si + "." + b64u(sig)

    # 场景1:回环 issuer 被 SSRF 拦截拒绝(安全行为)
    v_loop = OidcVerifier(issuer=ISSUER, jwks_fetch=True)
    claim1 = v_loop.verify({"id_token": token, "audience": "uai-hub"})
    check("E-OIDC-LIVE 回环 issuer 被 SSRF 拦截(安全行为)",
          claim1.authenticity == ASSERTED, str(claim1.reason)[:60])

    # 场景2:公网真实 IdP(Microsoft)拉取——验证 discovery/jwks 解析链路对真实厂商格式成立
    v_ms = OidcVerifier(issuer="https://login.microsoftonline.com/common/v2.0",
                        jwks_fetch=True, jwks_cache_s=60)
    jwks_ms = v_ms._fetch_jwks()
    check("E-OIDC-LIVE Microsoft discovery+jwks 拉取成功",
          isinstance(jwks_ms, dict) and len(jwks_ms.get("keys", [])) > 0,
          "%d keys" % len((jwks_ms or {}).get("keys", [])))
    check("E-OIDC-LIVE 拉取结果有缓存", v_ms._jwks_cache is not None, "")
    jwks_ms2 = v_ms._fetch_jwks()
    check("E-OIDC-LIVE 缓存命中(二次拉取同对象)",
          jwks_ms2 is jwks_ms, "")

    # 场景3:本机 IdP 全流程——给 OidcVerifier 注入 allow_loopback 测试开关后走真 discovery
    v_live = OidcVerifier(issuer=ISSUER, jwks_fetch=True, jwks_cache_s=60,
                          allow_loopback_for_test=True)
    claim3 = v_live.verify({"id_token": token, "audience": "uai-hub"})
    check("E-OIDC-LIVE 本机 IdP 全流程 authenticated",
          claim3.authenticity == AUTHENTICATED and claim3.subject == "live-user-1",
          str(claim3.reason)[:60])
    # 缓存生效:第二次验签不重新打 IdP(同对象)
    jw = v_live._fetch_jwks()
    check("E-OIDC-LIVE 本机 IdP JWKS 缓存命中", v_live._jwks_cache is not None and jw is v_live._jwks_cache[0], "")

    srv.shutdown()
    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
