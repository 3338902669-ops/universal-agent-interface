"""A-01 / C-03 自检：身份验证器与审批人真实性判定。

核心断言：**没有验证器就不得升级为 authenticated**；自称已认证也必须被验证器确认。
"""

from __future__ import annotations

import sys

import identity as I

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-60s %s" % ("PASS" if cond else "FAIL", name, detail))


def main() -> int:
    # 1. 无验证器 + 未声称认证 → asserted
    r = I.resolve_authenticity({})
    check("A-01 无验证器时默认 asserted", r["approver_authenticity"] == "asserted", str(r))

    # 2. 自称 authenticated 但没有验证器 → 不采信
    r = I.resolve_authenticity({"approver_authenticity": "authenticated"})
    check("C-03 自称已认证但无验证器 → 回落 asserted",
          r["approver_authenticity"] == "asserted" and "requires_verifier" in (r["reason"] or ""), str(r))

    # 3. 未配置的 OIDC → asserted（不得假装通过）
    oidc = I.OidcVerifier()
    r = I.resolve_authenticity({"approver_authenticity": "authenticated", "id_token": "a.b.c"}, oidc)
    check("C-03 未配置的 OIDC 验证器不给 authenticated",
          r["approver_authenticity"] == "asserted", str(r))

    # 4. 已配置的 OIDC 收到假 JWT → asserted + 明确拒因（2026-11-15 起为真验签实现：
    #    要么验签通过升 authenticated，要么具名拒绝，不再有未验签中间态）
    oidc2 = I.OidcVerifier(issuer="https://idp.example", jwks={"keys": []})
    r = I.resolve_authenticity({"approver_authenticity": "authenticated", "id_token": "a.b.c"}, oidc2)
    check("C-03 真 JWT 验签实现拒绝假令牌（不伪造结论）",
          r["approver_authenticity"] == "asserted" and bool(r["reason"]), str(r))

    # 5. 本地 token：即使匹配也只是 local，不构成跨域
    import hashlib
    tok = "local-secret"
    local = I.LocalTokenVerifier(hashlib.sha256(tok.encode()).hexdigest())
    r = I.resolve_authenticity({"token": tok, "subject": "dsh"}, local)
    check("A-01 本地 token 只证明本机席位，不构成跨域认证",
          r["approver_authenticity"] == "asserted" and r["issuer"] == "local", str(r))

    # 6. 本地 token 不匹配 → asserted + reason
    r = I.resolve_authenticity({"token": "wrong"}, local)
    check("A-01 token 不匹配 → asserted", r["approver_authenticity"] == "asserted"
          and "mismatch" in (r["reason"] or ""), str(r))

    # 7. 测试用 StaticVerifier 可给 authenticated，且必须带 subject+issuer
    sv = I.StaticVerifier()
    r = I.resolve_authenticity({"approver_authenticity": "authenticated"}, sv)
    check("C-03 已配置验证器可给 authenticated 且带 subject/issuer",
          r["approver_authenticity"] == "authenticated" and r["subject"] and r["issuer"], str(r))

    # 8. 验证器拒绝 → 降级
    r = I.resolve_authenticity({"approver_authenticity": "authenticated", "reject": True}, sv)
    check("C-03 验证器拒绝时降级为 asserted", r["approver_authenticity"] == "asserted", str(r))

    # 9. 非法值被拒
    try:
        I.resolve_authenticity({"approver_authenticity": "trusted"})
    except I.IdentityError as e:
        check("C-03 非法 authenticity 被拒", True, str(e)[:50])
    else:
        check("C-03 非法 authenticity 被拒", False, "未抛错")

    # 10. 声称 authenticated 但验证器返回缺 subject 的认证 → 抛错（规则 2）
    class Bad(I.IdentityVerifier):
        name = "bad"
        def __init__(self):
            super().__init__(); self.configured = True; self.config_error = None
        def _verify(self, c):
            return I.IdentityClaim(authenticity="authenticated")

    try:
        Bad().verify({})
    except I.IdentityError as e:
        check("A-01 认证结论缺 subject/issuer 被拒", True, str(e)[:50])
    else:
        check("A-01 认证结论缺 subject/issuer 被拒", False, "未抛错")

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
