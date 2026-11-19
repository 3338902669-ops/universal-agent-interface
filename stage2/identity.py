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

    def __init__(self, issuer: str | None = None, jwks: dict | None = None,
                 jwks_fetch: bool = False, jwks_cache_s: int = 300,
                 allow_loopback_for_test: bool = False):
        super().__init__()
        self.issuer = issuer
        self.jwks = jwks
        self.jwks_fetch = jwks_fetch          # True: 允许按 OIDC discovery 自动拉取 JWKS
        self.jwks_cache_s = jwks_cache_s      # 缓存秒数，防每验签一次打一次 IdP
        self.allow_loopback_for_test = allow_loopback_for_test  # 仅测试:允许回环 IdP
        self._jwks_cache: tuple[dict, float] | None = None
        self.configured = bool(issuer and (jwks or jwks_fetch))
        self.config_error = None if self.configured else "oidc_not_configured"

    def _fetch_jwks(self) -> dict | None:
        """OIDC discovery 自动拉取 JWKS：issuer/.well-known/openid-configuration → jwks_uri。

        拉取失败返回 None（→ verified 降级，不伪造结论）；缓存 jwks_cache_s 秒。
        限制：仅 http(s)；SSRF 防护——拒绝指向内网回环/链路本机的 issuer（本席验证器可能被
        用于验证不可信凭据，issuer 来自配置侧而非凭据侧，风险有限，仍做显式拦截）。
        """
        import urllib.request as _u
        import time as _t
        import json as _json
        if self._jwks_cache and (_t.time() - self._jwks_cache[1]) < self.jwks_cache_s:
            return self._jwks_cache[0]
        if not self.issuer or not self.issuer.startswith(("http://", "https://")):
            return None
        if not getattr(self, "allow_loopback_for_test", False):
            if "://127." in self.issuer or "://localhost" in self.issuer or "://[::1]" in self.issuer:
                return None  # SSRF 防护:验证器不得被不可信配置指到回环
        try:
            disc_url = self.issuer.rstrip("/") + "/.well-known/openid-configuration"
            with _u.urlopen(disc_url, timeout=8) as resp:
                jwks_uri = _json.loads(resp.read().decode("utf-8")).get("jwks_uri")
            if not jwks_uri or not jwks_uri.startswith(("http://", "https://")):
                return None
            with _u.urlopen(jwks_uri, timeout=8) as resp:
                jwks = _json.loads(resp.read().decode("utf-8"))
            if not isinstance(jwks.get("keys"), list):
                return None
            self._jwks_cache = (jwks, _t.time())
            return jwks
        except Exception:
            return None

    def _effective_jwks(self) -> dict | None:
        if self.jwks:
            return self.jwks
        if self.jwks_fetch:
            return self._fetch_jwks()
        return None

    def _verify(self, credential: dict) -> IdentityClaim:
        """RS256/ES256 JWT 验签 + exp/nbf/iss/aud 校验（2026-11-15 真实现）。"""
        import base64
        import json as _json
        import time as _time

        token = credential.get("id_token")
        if not token:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="missing_id_token")
        parts = str(token).split(".")
        if len(parts) != 3:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="malformed_jwt")

        def _b64d(seg):
            pad = "=" * (-len(seg) % 4)
            return base64.urlsafe_b64decode(seg + pad)

        try:
            header = _json.loads(_b64d(parts[0]))
            payload = _json.loads(_b64d(parts[1]))
        except Exception as e:
            return IdentityClaim(authenticity=ASSERTED, method=self.name,
                                 reason="jwt_decode_failed:%s" % str(e)[:60])

        alg = header.get("alg")
        kid = header.get("kid")
        if alg not in ("RS256", "ES256"):
            return IdentityClaim(authenticity=ASSERTED, method=self.name,
                                 reason="unsupported_alg:%s" % alg)

        keys = [k for k in (self._effective_jwks() or {}).get("keys", [])
                if isinstance(k, dict) and k.get("kty") in ("RSA", "EC")]
        if kid:
            keys = [k for k in keys if k.get("kid") == kid]
            if not keys:
                return IdentityClaim(authenticity=ASSERTED, method=self.name,
                                     reason="jwks_key_not_found:%s" % kid)
        same = [k for k in keys if (k.get("kty") == "RSA") == (alg == "RS256")]
        if len(same) != 1:
            return IdentityClaim(authenticity=ASSERTED, method=self.name,
                                 reason="jwks_key_not_found:%s" % (kid or "default"))
        jwk = same[0]

        signing_input = (parts[0] + "." + parts[1]).encode("ascii")
        sig = _b64d(parts[2])
        try:
            if alg == "RS256":
                from cryptography.hazmat.primitives.asymmetric import rsa, padding as _pad
                from cryptography.hazmat.primitives import hashes as _hashes
                n = int.from_bytes(_b64d(jwk["n"]), "big")
                e = int.from_bytes(_b64d(jwk["e"]), "big")
                pub = rsa.RSAPublicNumbers(e, n).public_key()
                pub.verify(sig, signing_input, _pad.PKCS1v15(), _hashes.SHA256())
            else:
                from cryptography.hazmat.primitives.asymmetric import ec
                from cryptography.hazmat.primitives import hashes as _hashes2
                x = int.from_bytes(_b64d(jwk["x"]), "big")
                y = int.from_bytes(_b64d(jwk["y"]), "big")
                crv = {"P-256": ec.SECP256R1()}.get(jwk.get("crv"))
                if crv is None:
                    return IdentityClaim(authenticity=ASSERTED, method=self.name,
                                         reason="unsupported_crv:%s" % jwk.get("crv"))
                pub = ec.EllipticCurvePublicNumbers(x, y, crv).public_key()
                pub.verify(sig, signing_input, ec.ECDSA(_hashes2.SHA256()))
        except Exception:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="jwt_signature_invalid")

        now = int(_time.time())
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)) or now >= int(exp):
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="jwt_expired_or_no_exp")
        nbf = payload.get("nbf")
        if isinstance(nbf, (int, float)) and now < int(nbf):
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="jwt_not_yet_valid")
        if self.issuer and payload.get("iss") != self.issuer:
            return IdentityClaim(authenticity=ASSERTED, method=self.name,
                                 reason="issuer_mismatch:%s" % payload.get("iss"))
        aud = payload.get("aud")
        expected_aud = credential.get("audience")
        if isinstance(expected_aud, str):
            ok_aud = (aud == expected_aud) if isinstance(aud, str) else (
                isinstance(aud, list) and expected_aud in aud)
            if not ok_aud:
                return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="audience_mismatch")

        sub = payload.get("sub")
        if not isinstance(sub, str) or not sub:
            return IdentityClaim(authenticity=ASSERTED, method=self.name, reason="missing_sub")
        return IdentityClaim(authenticity=AUTHENTICATED, subject=sub,
                             issuer=payload.get("iss") or self.issuer or "", method=self.name)


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


class Ed25519Verifier(IdentityVerifier):
    """真验证器（2026-11-15）：Ed25519 签名凭据，验签通过才升 authenticated。

    凭据形状：{"subject", "issuer", "pubkey_hex", "sig_hex", "issued_at"}
    message 规范串 = "uai-identity-v1:" + subject + ":" + issuer + ":" + issued_at
    pubkey 需与构造时注入的 trusted_pubkey_hex 一致（信任锚），防任意自签公钥。
    """
    name = "ed25519"

    def __init__(self, trusted_pubkey_hex: str | None = None):
        super().__init__()
        self.trusted = (trusted_pubkey_hex or "").lower() or None
        self.configured = bool(self.trusted)
        self.config_error = None if self.configured else "no_trusted_pubkey"

    def _verify(self, credential: dict) -> IdentityClaim:
        subject = credential.get("subject")
        issuer = credential.get("issuer")
        issued_at = credential.get("issued_at")
        pubkey_hex = credential.get("pubkey_hex")
        sig_hex = credential.get("sig_hex")
        if not (isinstance(subject, str) and subject and isinstance(issuer, str) and issuer):
            raise IdentityError("missing_subject_or_issuer")
        if not (isinstance(pubkey_hex, str) and isinstance(sig_hex, str)):
            raise IdentityError("missing_signature_material")
        if self.trusted and pubkey_hex.lower() != self.trusted:
            raise IdentityError("untrusted_pubkey")
        message = ("uai-identity-v1:%s:%s:%s" % (subject, issuer, issued_at or "")).encode("utf-8")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(pubkey_hex))
        try:
            pub.verify(bytes.fromhex(sig_hex), message)
        except Exception as e:
            raise IdentityError("signature_invalid") from e
        return IdentityClaim(authenticity=AUTHENTICATED, subject=subject,
                             issuer=issuer, method=self.name)


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
