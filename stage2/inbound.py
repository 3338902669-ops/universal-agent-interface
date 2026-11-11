"""Stage 2 入站转换：A2A 请求 → 本契约 v1 任务信封。

契约依据：`protocol/contract-v1.md` §3（信封 11 必填 + 6 可选、target 二选一、≤262144 字节、
`cls=external` 必带审批）。映射依据：`~/.shared/A2A-SPEC-MAPPING.md` v1.4 §3、§7.3。

与映射表 §7.3 的一处修正：
  原文说幂等键按 `subject + task_id + instruction + params` 派生。但**入站时 task_id 还不存在**
  （由本契约侧生成），拿它当锚点会让每次重试都变成新键。改为以 A2A 的 `messageId` 为锚：
      idempotency_key = sha256(subject + ":" + message_id + ":" + canonical(instruction) + ":" + canonical(params))
  同一 messageId 重投 → 同一键；改了正文或参数 → 不同键。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

CONTRACT_VERSION = "1.0"          # 当前契约版本（v1）
SUPPORTED_CONTRACT_VERSIONS = ("1.0", "0.2")   # 0.2 用于对接旧版实验室原型（版本协商，见 A-05）
MAX_ENVELOPE_BYTES = 262144
VALID_CLS = ("readonly", "reversible", "external")
VALID_PRIORITY = ("high", "standard", "low")


class InboundError(ValueError):
    """请求无法转成合法信封。"""


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def derive_idempotency_key(subject: str, message_id: str, instruction: str, params) -> str:
    raw = ":".join([subject, message_id, instruction, _canonical(params)])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _text_of(parts: list[dict]) -> str:
    texts = [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if not texts:
        raise InboundError("missing_text_part")
    return "\n".join(texts)


def _attachments_of(parts: list[dict]) -> list[dict]:
    """A2A 的 Part 没有 sha256 字段，指纹必须在 metadata["uai/sha256"] 里给出。"""
    out = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        if "url" not in p and "raw" not in p:
            continue
        meta = p.get("metadata") or {}
        sha = meta.get("uai/sha256")
        size = meta.get("uai/bytes")
        if not sha:
            raise InboundError("attachment_missing_sha256: A2A Part 无 sha256 字段，须在 metadata[uai/sha256] 提供")
        if len(str(sha)) != 64:
            raise InboundError("attachment_bad_sha256: %r" % (sha,))
        if size is None:
            raise InboundError("attachment_missing_bytes")
        out.append(
            {
                "uri": p.get("url") or "",
                "sha256": str(sha).lower(),
                "bytes": int(size),
                "media_type": p.get("media_type") or "application/octet-stream",
            }
        )
    return out


def _params_of(parts: list[dict]) -> dict | None:
    for p in parts:
        if isinstance(p, dict) and isinstance(p.get("data"), dict):
            return p["data"]
    return None


def envelope_from_a2a(
    request: dict,
    *,
    created_by: str,
    target_interface: str | None = None,
    target_capability: str | None = None,
    timeout_s: int = 300,
    cls: str = "readonly",
    priority: str = "standard",
    resource_key: str | None = None,
    skill_ref: dict | None = None,
    approval: dict | None = None,
    task_id: str | None = None,
    contract_version: str = CONTRACT_VERSION,
) -> dict:
    """A2A 请求 → 契约 v1 信封。不合法一律抛 InboundError，不静默降级。"""
    if not isinstance(request, dict):
        raise InboundError("request_not_object")

    message = request.get("message") or request
    parts = message.get("parts")
    if not isinstance(parts, list) or not parts:
        raise InboundError("missing_parts")

    message_id = message.get("messageId") or message.get("message_id")
    if not message_id:
        raise InboundError("missing_message_id")

    subject = request.get("subject") or created_by

    # target 二选一，且必须恰好一个
    if bool(target_interface) == bool(target_capability):
        raise InboundError("target_must_name_exactly_one")

    if cls not in VALID_CLS:
        raise InboundError("invalid_cls: %r" % (cls,))
    if cls == "external" and not approval:
        raise InboundError("external_requires_approval")
    if priority not in VALID_PRIORITY:
        raise InboundError("invalid_priority: %r" % (priority,))

    if not isinstance(timeout_s, int) or timeout_s <= 0:
        raise InboundError("invalid_timeout_s: %r" % (timeout_s,))

    instruction = _text_of(parts)
    params = _params_of(parts)
    attachments = _attachments_of(parts)

    if contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        raise InboundError("unsupported_contract_version: %r（支持 %s）"
                           % (contract_version, SUPPORTED_CONTRACT_VERSIONS))

    envelope = {
        "contract_version": contract_version,
        "envelope_id": str(uuid.uuid4()),
        "task_id": task_id or str(uuid.uuid4()),
        "idempotency_key": derive_idempotency_key(subject, message_id, instruction, params),
        "target": {"interface_id": target_interface} if target_interface else {"capability": target_capability},
        "instruction": instruction,
        "timeout_s": timeout_s,
        "cls": cls,
        "priority": priority,
        "approval": approval,
        "created_by": created_by,
        "created_at": _now_iso(),
        "trace_id": str(uuid.uuid4()),
        "skill_ref": skill_ref,
        "resource_key": resource_key,
        "params": params,
        "attachments": attachments,
    }

    size = len(json.dumps(envelope, ensure_ascii=False).encode("utf-8"))
    if size > MAX_ENVELOPE_BYTES:
        raise InboundError("envelope_too_large: %d > %d" % (size, MAX_ENVELOPE_BYTES))

    # 不得夹带凭据字样（契约 §3：params 不得放密钥/口令/明文凭据）
    blob = json.dumps(envelope, ensure_ascii=False).lower()
    for bad in ("api_key", "apikey", "password", "secret", "token="):
        if bad in blob:
            raise InboundError("credential_like_field_in_envelope: %s" % bad)

    return envelope
