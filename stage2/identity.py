"""跨域身份（A-01 / C-03）：可插拔身份验证器。

问题：当前审批人身份只能是 `asserted`（AO 的 A4 为 OUT OF SCOPE，GAR 的 F-GAR-08 同项）。
跨厂商协作的前提是"我知道你是哪家的哪个 agent，且这个声明可验证"。

本模块给的是**接口与判定规则**，不是某个 IdP 的绑定：

    verifier = LocalTokenVerifier()          # 现状：单域 token
    verifier = OidcVerifier(issuer=..., jwks=...)   # 跨域：未配置时不得假装通过
    verifier = MTLSVerifier(ca=...)                 # 跨域：双向证书

三条硬规则：
  1. **默认是 `asserted`**。没有验证器或验证器未配置时，不得升级为 `authenticated`。
  2. **验证器只能"确认"，不能"创造"**。它返回 authenticated 必须带 subject 与 issuer。
  3. **失败即降级**。验证器报错 → 回落 `asserted`，并在审批记录里留下 reason。
"""

from __future__ import annotations

from dataclasses import dataclass, field

ASSERTED = "asserted"
AUTHENTICATED = "authenticated"
VALID_AUTHENTICITY = (ASSERTED, AUTHENTICATED)


class IdentityError(ValueError):
    pass


@dataclass
class IdentityClaim:
    """一次身份验证的结果。"""
    authenticity: str = ASSERTED
    subject: str | None = None
    issuer: str | None = None
    method: str = "none"
    reason: str | None = None
    evidence: dict = field(default_factory=dict)

    def as_record(self) -> dict:
        return {
            "approver_authenticity": self.authenticity,
            "subject": self.subject,
            "issuer": self.issuer,
            "method": self.method,
            "reason": self.reason,
        }


class IdentityVerifier:
    """验证器基类。子类必须实现 `_verify`。"""

    name = "base"

    def __init__(self):
        self.configured = False
        self.config_error: str | None = "not_configured"

    def verify(self, credential: dict) -> IdentityClaim:
        if not self.configured:
            return IdentityClaim(authenticity=ASSERTED, method=self.name,
                                 reason=self.config_error or "not_configured")
        try:
            claim = self._verify(credential)
        except Exception as e:  # 规则 3：失败即降级
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason=str(e)[:120])
        if claim.authenticity == AUTHENTICATED and not (claim.subject and claim.issuer):
            raise IdentityError("authenticated_claim_requires_subject_and_issuer")
        return claim

    def _verify(self, credential: dict) -> IdentityClaim:  # pragma: no cover
        raise NotImplementedError


class LocalTokenVerifier(IdentityVerifier):
    """单域：本地 token 哈希。只能证明"持有本机 token"，不构成跨域身份。"""
    name = "local-token"

    def __init__(self, expected_token_sha256: str | None = None):
        super().__init__()
        self.expected = (expected_token_sha256 or "").lower() or None
        self.configured = bool(self.expected)
        self.config_error = None if self.configured else "no_local_token_configured"

    def _verify(self, credential: dict) -> IdentityClaim:
        import hashlib

        token = credential.get("token")
        if not token:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="missing_token")
        got = hashlib.sha256(token.encode("utf-8")).hexdigest()
        if got != self.expected:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="token_mismatch")
        # 即使 token 正确，也只是"本机席位"，issuer 固定为 local，不构成跨域认证
        return IdentityClaim(authenticity=ASSERTED, subject=credential.get("subject"),
                             issuer="local", method=self.name, reason="local_token_ok")


class OidcVerifier(IdentityVerifier):
    """跨域：OIDC/JWT。未注入 JWKS 时不得通过（规则 1）。"""
    name = "oidc"

    def __init__(self, issuer: str | None = None, jwks: dict | None = None):
        super().__init__()
        self.issuer = issuer
        self.jwks = jwks
        self.configured = bool(issuer and jwks)
        self.config_error = None if self.configured else "oidc_not_configured"

    def _verify(self, credential: dict) -> IdentityClaim:
        token = credential.get("id_token")
        if not token:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="missing_id_token")
        # 真实实现需验签；此处只做结构判定，避免在无 JWKS 时假装已验证
        parts = str(token).split(".")
        if len(parts) != 3:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="malformed_jwt")
        return IdentityClaim(authenticity=ASSERTED, method=self.name,
                             reason="jwt_signature_not_verified_in_reference_impl")


class MTLSVerifier(IdentityVerifier):
    """跨域：双向证书。未提供 CA 时不得通过。"""
    name = "mtls"

    def __init__(self, ca_pem: str | None = None):
        super().__init__()
        self.ca_pem = ca_pem
        self.configured = bool(ca_pem)
        self.config_error = None if self.configured else "mtls_ca_not_configured"

    def _verify(self, credential: dict) -> IdentityClaim:
        cn = credential.get("client_cn")
        if not cn:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="missing_client_certificate")
        return IdentityClaim(authenticity=ASSERTED, method=self.name,
                             reason="certificate_chain_not_verified_in_reference_impl")


class StaticVerifier(IdentityVerifier):
    """仅用于测试：显式声明为已认证。生产不得使用。"""
    name = "static-test"

    def __init__(self, subject: str = "test-subject", issuer: str = "test-issuer"):
        super().__init__()
        self.subject = subject
        self.issuer = issuer
        self.configured = True
        self.config_error = None
        self.used = False

    def _verify(self, credential: dict) -> IdentityClaim:
        self.used = True
        if credential.get("reject"):
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="rejected_by_test")
        return IdentityClaim(authenticity=AUTHENTICATED, subject=self.subject,
                             issuer=self.issuer, method=self.name)


def resolve_authenticity(credential: dict, verifier: IdentityVerifier | None = None) -> dict:
    """把凭据里的 approver_authenticity 与验证器结论合并。默认断言，绝不自行升级。"""
    claimed = (credential or {}).get("approver_authenticity", ASSERTED)
    if claimed not in VALID_AUTHENTICITY:
        raise IdentityError("bad_approver_authenticity: %r" % (claimed,))
    if claimed == AUTHENTICATED:
        # 自称已认证但没有验证器 → 不接受
        if verifier is None:
            return IdentityClaim(authenticity=ASSERTED, method="none",
                                 reason="self_asserted_authenticated_requires_verifier").as_record()
        claim = verifier.verify(credential)
        if claim.authenticity != AUTHENTICATED:
            return claim.as_record()
        return claim.as_record()
    if verifier is None:
        return IdentityClaim(authenticity=ASSERTED, method="none",
                             reason="no_verifier_configured").as_record()
    return verifier.verify(credential).as_record()
