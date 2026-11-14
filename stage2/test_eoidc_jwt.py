"""E-OIDC:OidcVerifier 真实现测试(RS256 JWT + JWK + exp/iss/aud)。

密钥对在测试内生成,JWT 按标准 b64url 三段签名;不依赖网络 IdP。
"""
from __future__ import annotations

import base64
import json
import sys
import time

from identity import OidcVerifier, ASSERTED, AUTHENTICATED

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def main() -> int:
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    nums = key.public_key().public_numbers()
    n_b = nums.n.to_bytes((nums.n.bit_length() + 7) // 8, "big")
    e_b = nums.e.to_bytes(3, "big")
    jwks = {"keys": [{"kty": "RSA", "kid": "k1", "n": b64u(n_b), "e": b64u(e_b)}]}

    now = int(time.time())
    header = {"alg": "RS256", "kid": "k1", "typ": "JWT"}

    def make_jwt(payload, sig_key=None):
        si = b64u(json.dumps(header).encode()) + "." + b64u(json.dumps(payload).encode())
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives import hashes
        sk = sig_key or key
        sig = sk.sign(si.encode(), padding.PKCS1v15(), hashes.SHA256())
        return si + "." + b64u(sig)

    base_payload = {"iss": "https://idp.example.com", "sub": "user-42",
                    "aud": "uai-hub", "exp": now + 600, "iat": now}
    good = make_jwt(base_payload)
    v = OidcVerifier(issuer="https://idp.example.com", jwks=jwks)
    claim = v.verify({"id_token": good, "audience": "uai-hub"})
    check("E-OIDC 有效 JWT 升 authenticated",
          claim.authenticity == AUTHENTICATED and claim.subject == "user-42"
          and claim.issuer == "https://idp.example.com", claim.authenticity)

    # 错签
    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    claim2 = v.verify({"id_token": make_jwt(base_payload, sig_key=rogue), "audience": "uai-hub"})
    check("E-OIDC 错签拒绝留 reason",
          claim2.authenticity == ASSERTED and claim2.reason == "jwt_signature_invalid",
          str(claim2.reason))

    # 过期
    expired = make_jwt({**base_payload, "exp": now - 10})
    claim3 = v.verify({"id_token": expired, "audience": "uai-hub"})
    check("E-OIDC 过期拒绝", claim3.authenticity == ASSERTED
          and claim3.reason == "jwt_expired_or_no_exp", str(claim3.reason))

    # iss 不匹配
    claim4 = v.verify({"id_token": make_jwt({**base_payload, "iss": "https://evil.example"}),
                       "audience": "uai-hub"})
    check("E-OIDC issuer 不匹配拒绝",
          claim4.authenticity == ASSERTED and claim4.reason.startswith("issuer_mismatch"),
          str(claim4.reason)[:50])

    # aud 不匹配
    claim5 = v.verify({"id_token": good, "audience": "other-service"})
    check("E-OIDC audience 不匹配拒绝", claim5.authenticity == ASSERTED
          and claim5.reason == "audience_mismatch", str(claim5.reason))

    # 未配置不升级
    v0 = OidcVerifier()
    claim6 = v0.verify({"id_token": good})
    check("E-OIDC 未配置不升级", claim6.authenticity == ASSERTED
          and claim6.reason == "oidc_not_configured", str(claim6.reason))

    # kid 不存在
    h2 = {"alg": "RS256", "kid": "nope", "typ": "JWT"}
    si2 = b64u(json.dumps(h2).encode()) + "." + b64u(json.dumps(base_payload).encode())
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    sig2 = b64u(key.sign(si2.encode(), padding.PKCS1v15(), hashes.SHA256()))
    claim7 = v.verify({"id_token": si2 + "." + sig2, "audience": "uai-hub"})
    check("E-OIDC kid 不存在拒绝", claim7.authenticity == ASSERTED
          and claim7.reason.startswith("jwks_key_not_found"), str(claim7.reason)[:50])

    # ES256 路径:P-256 EC 密钥
    from cryptography.hazmat.primitives.asymmetric import ec
    eck = ec.generate_private_key(ec.SECP256R1())
    en = eck.public_key().public_numbers()
    x_b = en.x.to_bytes(32, "big"); y_b = en.y.to_bytes(32, "big")
    jwks_ec = {"keys": [{"kty": "EC", "kid": "e1", "crv": "P-256", "x": b64u(x_b), "y": b64u(y_b)}]}
    h3 = {"alg": "ES256", "kid": "e1", "typ": "JWT"}
    si3 = b64u(json.dumps(h3).encode()) + "." + b64u(json.dumps(base_payload).encode())
    sig3 = eck.sign(si3.encode(), ec.ECDSA(hashes.SHA256()))
    ve = OidcVerifier(issuer="https://idp.example.com", jwks=jwks_ec)
    claim8 = ve.verify({"id_token": si3 + "." + b64u(sig3), "audience": "uai-hub"})
    check("E-OIDC ES256(P-256) 路径升 authenticated", claim8.authenticity == AUTHENTICATED,
          claim8.authenticity)

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
