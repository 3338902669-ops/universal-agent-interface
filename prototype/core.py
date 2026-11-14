"""Shared primitives for the v0.2 interface prototype.

This module contains contract validation, authenticated request helpers, and
append-only JSONL utilities.  It never persists authentication token values.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


CONTRACT_VERSION = "1.0"
SUPPORTED_CONTRACT_VERSIONS = ("1.0", "0.2")
MAX_ENVELOPE_BYTES = 256 * 1024  # v0.2 与 v1 均为 262,144 字节
# v1（contract-v1.md §3）：11 必填 + 6 可选
REQUIRED_ENVELOPE_FIELDS_V1 = {
    "contract_version",
    "envelope_id",
    "task_id",
    "idempotency_key",
    "target",
    "instruction",
    "timeout_s",
    "cls",
    "created_by",
    "created_at",
    "trace_id",
}
# v0.2 遗留（spec-v0.2 §4）：13 必填
REQUIRED_ENVELOPE_FIELDS_V02 = {
    "contract_version",
    "task_id",
    "idempotency_key",
    "target",
    "instruction",
    "skill_ref",
    "resource_key",
    "params",
    "attachments",
    "timeout_s",
    "cls",
    "created_by",
    "created_at",
    "trace_id",
}
VALID_PRIORITIES = {"high", "standard", "low"}
VALID_CLASSES = {"readonly", "reversible", "external"}
VALID_EVENT_KINDS = {
    "accepted",
    "claimed",
    "progress",
    "heartbeat",
    "result_ready",
    "completed",
    "failed",
    "cancelled",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def sign_value(token: str, value: Any) -> str:
    return hmac.new(
        token.encode("utf-8"), canonical_json(value), hashlib.sha256
    ).hexdigest()


def verify_value(token: str, value: Any, proof: str) -> bool:
    return hmac.compare_digest(sign_value(token, value), proof)


def make_proof(token: str, request: dict[str, Any]) -> dict[str, Any]:
    clean = dict(request)
    clean.pop("proof", None)
    clean["proof"] = sign_value(token, clean)
    return clean


def verify_proof(token: str, request: dict[str, Any]) -> bool:
    proof = request.get("proof")
    if not isinstance(proof, str):
        return False
    clean = dict(request)
    clean.pop("proof", None)
    return verify_value(token, clean, proof)


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rows.append(json.loads(line))
    return rows


class AppendOnlyJsonl:
    """Thread-safe JSONL appender with flush+fsync on every record."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def append(self, record: dict[str, Any]) -> None:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def records(self) -> list[dict[str, Any]]:
        with self._lock:
            return read_jsonl(self.path)


def append_log(path: str | Path, component: str, message: str, **fields: Any) -> None:
    """Append a log record after rejecting suspicious secret-bearing keys."""

    forbidden = {
        "token",
        "secret",
        "password",
        "authorization",
        "api_key",
        "credential",
        "proof",
    }
    for key in fields:
        if key.lower() in forbidden:
            raise ValueError(f"refusing to log sensitive field {key!r}")
    record = {"ts": now_iso(), "component": component, "msg": message, **fields}
    AppendOnlyJsonl(path).append(record)


def file_uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError("lab attachment/result URIs must use file://")
    if parsed.netloc not in ("", "localhost"):
        raise ValueError("non-local file URI host is not allowed")
    raw = unquote(parsed.path)
    if os.name == "nt" and raw.startswith("/") and len(raw) > 2 and raw[2] == ":":
        raw = raw[1:]
    return Path(raw)


def path_to_file_uri(path: str | Path) -> str:
    return Path(path).resolve().as_uri()


def validate_envelope(raw: bytes | str | dict[str, Any]) -> tuple[bool, str | None, dict[str, Any] | None]:
    """Validate a task envelope without mutating it."""

    if isinstance(raw, (bytes, bytearray)):
        if len(raw) > MAX_ENVELOPE_BYTES:
            return False, "envelope_too_large", None
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False, "invalid_json", None
    elif isinstance(raw, str):
        if len(raw.encode("utf-8")) > MAX_ENVELOPE_BYTES:
            return False, "envelope_too_large", None
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return False, "invalid_json", None
    else:
        value = raw

    if not isinstance(value, dict):
        return False, "not_object", None
    version = value.get("contract_version")
    if version == "1.0":
        required = REQUIRED_ENVELOPE_FIELDS_V1
        optional = {"skill_ref", "resource_key", "params", "attachments",
                    "priority", "approval", "context_id"}
    elif version == "0.2":
        required = REQUIRED_ENVELOPE_FIELDS_V02
        optional = set()
    else:
        return False, "unsupported_contract_version", None
    missing = sorted(required - value.keys())
    if missing:
        return False, "missing_fields:" + ",".join(missing), None
    unknown = sorted(set(value.keys()) - required - optional)
    if unknown:
        return False, "unknown_fields:" + ",".join(unknown), None
    if version == "1.0":
        envelope_id = value.get("envelope_id")
        if not isinstance(envelope_id, str) or not envelope_id:
            return False, "invalid_envelope_id", None
        priority = value.get("priority")
        if priority is not None and priority not in VALID_PRIORITIES:
            return False, "invalid_priority", None
        # v1：可选字段缺省为 None 时按缺省处理；attachments 提供了才校验
        if value.get("attachments") is not None and not isinstance(value["attachments"], list):
            return False, "invalid_attachments", None
    elif not isinstance(value["attachments"], list):
        return False, "invalid_attachments", None
    try:
        uuid.UUID(str(value["task_id"]))
    except (ValueError, TypeError, AttributeError):
        return False, "invalid_task_id", None
    if not isinstance(value["idempotency_key"], str) or not value["idempotency_key"]:
        return False, "invalid_idempotency_key", None
    target = value["target"]
    if not isinstance(target, dict):
        return False, "invalid_target", None
    if bool(target.get("interface_id")) == bool(target.get("capability")):
        return False, "target_must_name_exactly_one", None
    if not isinstance(value["instruction"], str):
        return False, "invalid_instruction", None
    if value["cls"] not in VALID_CLASSES:
        return False, "invalid_cls", None
    if not isinstance(value["timeout_s"], (int, float)) or value["timeout_s"] <= 0:
        return False, "invalid_timeout_s", None
    if value["resource_key"] is not None and not isinstance(value["resource_key"], str):
        return False, "invalid_resource_key", None
    if not isinstance(value["attachments"], list):
        return False, "invalid_attachments", None
    for attachment in (value.get("attachments") or []):
        if not isinstance(attachment, dict):
            return False, "invalid_attachment", None
        uri = attachment.get("uri")
        expected = attachment.get("sha256")
        if not isinstance(uri, str) or not isinstance(expected, str):
            return False, "invalid_attachment_fields", None
        try:
            path = file_uri_to_path(uri)
        except ValueError:
            return False, "unsupported_attachment_uri", None
        if not path.is_file():
            return False, "attachment_missing", None
        if sha256_file(path) != expected.lower():
            return False, "attachment_hash_mismatch", None
    encoded = canonical_json(value)
    if len(encoded) > MAX_ENVELOPE_BYTES:
        return False, "envelope_too_large", None
    return True, None, value


def envelope_digest(envelope: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(envelope))


def atomic_write_json(path: str | Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
