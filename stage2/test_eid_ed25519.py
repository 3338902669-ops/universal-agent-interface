"""E-ID:Ed25519Verifier 真验证器测试。

验证:有效签名升 authenticated;错签/错公钥/缺字段拒绝并留 reason;未配置不升级。
"""
from __future__ import annotations

import sys

from identity import Ed25519Verifier, ASSERTED, AUTHENTICATED

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    # 信任锚密钥对
    trust = Ed25519PrivateKey.generate()
    trust_pub = trust.public_key().public_bytes_raw().hex()
    subject, issuer, issued_at = "approver-a", "corp-idp", "2026-11-15T00:00:00Z"
    message = ("uai-identity-v1:%s:%s:%s" % (subject, issuer, issued_at)).encode()
    sig = trust.sign(message).hex()

    # 1. 有效签名 → authenticated
    v = Ed25519Verifier(trusted_pubkey_hex=trust_pub)
    claim = v.verify({"subject": subject, "issuer": issuer, "issued_at": issued_at,
                      "pubkey_hex": trust_pub, "sig_hex": sig})
    check("E-ID 有效签名升 authenticated",
          claim.authenticity == AUTHENTICATED and claim.subject == subject
          and claim.issuer == issuer, claim.authenticity)

    # 2. 错签（另一把私钥）→ 拒绝降级留 reason
    rogue = Ed25519PrivateKey.generate()
    bad_sig = rogue.sign(message).hex()
    claim2 = v.verify({"subject": subject, "issuer": issuer, "issued_at": issued_at,
                       "pubkey_hex": trust_pub, "sig_hex": bad_sig})
    check("E-ID 错签降级 asserted 且留 reason",
          claim2.authenticity == ASSERTED and bool(claim2.reason), str(claim2.reason)[:60])

    # 3. 不可信公钥（自签但非信任锚）→ 拒绝
    rogue_pub = rogue.public_key().public_bytes_raw().hex()
    rogue_sig = rogue.sign(message).hex()
    claim3 = v.verify({"subject": subject, "issuer": issuer, "issued_at": issued_at,
                       "pubkey_hex": rogue_pub, "sig_hex": rogue_sig})
    check("E-ID 非信任锚公钥拒绝", claim3.authenticity == ASSERTED, str(claim3.reason)[:60])

    # 4. 消息被篡改（subject 换人）→ 验签失败
    tampered = ("uai-identity-v1:%s:%s:%s" % ("someone-else", issuer, issued_at)).encode()
    claim4 = v.verify({"subject": "someone-else", "issuer": issuer, "issued_at": issued_at,
                       "pubkey_hex": trust_pub, "sig_hex": sig})
    check("E-ID 篡改 subject 拒绝", claim4.authenticity == ASSERTED, str(claim4.reason)[:60])

    # 5. 未配置（无信任锚）→ asserted + not_configured
    v0 = Ed25519Verifier()
    claim5 = v0.verify({"subject": subject, "issuer": issuer})
    check("E-ID 未配置不升级", claim5.authenticity == ASSERTED
          and claim5.reason == "no_trusted_pubkey", str(claim5.reason))

    # 6. 过期语义：issued_at 为空 → message 缺时间成分,信任锚签名不符 → 拒绝
    claim6 = v.verify({"subject": subject, "issuer": issuer, "issued_at": None,
                       "pubkey_hex": trust_pub, "sig_hex": sig})
    check("E-ID issued_at 缺失致验签不符拒绝", claim6.authenticity == ASSERTED, str(claim6.reason)[:60])

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
