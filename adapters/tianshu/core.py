"""Shared primitives for the v0.2 interface prototype.

This module contains contract validation, authenticated request helpers, and
append-only JSONL utilities.  It never persists authentication token values.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse


CONTRACT_VERSION = "0.2"
MAX_ENVELOPE_BYTES = 256 * 1024
REQUIRED_ENVELOPE_FIELDS = {
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
APPROVAL_VERSION = "approval-v0.2"
APPROVAL_FIELDS = {
    "version",
    "credential_id",
    "task_id",
    "trace_id",
    "action_id",
    "decision",
    "binding_sha256",
    "issued_at",
    "expires_at",
    "single_envelope",
    "signature",
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


def load_or_create_secret(path: str | Path) -> str:
    """Load a lab signing secret, creating it atomically with owner-only intent.

    The value is never returned by a tool or written to a log.  It is used only
    inside the lab processes that sign and verify one-envelope approval grants.
    """

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        value = path.read_text(encoding="utf-8").strip()
        if len(value) < 32:
            raise ValueError("approval signing secret is too short")
        return value
    except FileNotFoundError:
        pass

    generated = secrets.token_urlsafe(48)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(generated + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        raise
    return generated


def approval_binding_sha256(envelope: dict[str, Any]) -> str:
    """Hash the task envelope while excluding the approval grant itself."""

    bound = copy.deepcopy(envelope)
    params = bound.get("params")
    if isinstance(params, dict):
        params.pop("approval", None)
    return sha256_bytes(canonical_json(bound))


def sign_approval_grant(
    secret: str, grant: dict[str, Any]
) -> dict[str, Any]:
    clean = {key: value for key, value in grant.items() if key != "signature"}
    clean["signature"] = sign_value(secret, clean)
    return clean


def validate_approval_grant(
    envelope: dict[str, Any],
    secret: str,
    *,
    now: datetime | None = None,
) -> tuple[bool, str]:
    """Validate a lab approval grant against one exact envelope and action."""

    if not isinstance(envelope, dict) or envelope.get("cls") != "external":
        return False, "approval_external_only"
    params = envelope.get("params")
    if not isinstance(params, dict):
        return False, "approval_missing"
    grant = params.get("approval")
    if not isinstance(grant, dict):
        return False, "approval_missing"
    missing = sorted(APPROVAL_FIELDS - grant.keys())
    if missing:
        return False, "approval_missing_fields:" + ",".join(missing)
    if grant.get("version") != APPROVAL_VERSION:
        return False, "approval_version"
    if grant.get("task_id") != envelope.get("task_id"):
        return False, "approval_task_mismatch"
    if grant.get("trace_id") != envelope.get("trace_id"):
        return False, "approval_trace_mismatch"
    if grant.get("action_id") != params.get("action_id"):
        return False, "approval_action_mismatch"
    if grant.get("binding_sha256") != approval_binding_sha256(envelope):
        return False, "approval_binding_mismatch"
    if grant.get("single_envelope") is not True:
        return False, "approval_not_envelope_scoped"
    if not isinstance(grant.get("decision"), str) or not grant.get("decision"):
        return False, "approval_decision_missing"
    if not isinstance(grant.get("credential_id"), str) or not grant.get(
        "credential_id"
    ):
        return False, "approval_id_missing"
    try:
        issued_at = datetime.fromisoformat(
            str(grant["issued_at"]).replace("Z", "+00:00")
        )
        expires_at = datetime.fromisoformat(
            str(grant["expires_at"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError):
        return False, "approval_time_invalid"
    reference = now or datetime.now(timezone.utc)
    if issued_at > reference:
        return False, "approval_not_yet_valid"
    if expires_at <= reference:
        return False, "approval_expired"
    clean = {key: value for key, value in grant.items() if key != "signature"}
    if not verify_value(secret, clean, str(grant.get("signature", ""))):
        return False, "approval_signature_invalid"
    return True, "approval_valid"


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
    missing = sorted(REQUIRED_ENVELOPE_FIELDS - value.keys())
    if missing:
        return False, "missing_fields:" + ",".join(missing), None
    if value["contract_version"] != CONTRACT_VERSION:
        return False, "unsupported_contract_version", None
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
    for attachment in value["attachments"]:
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
