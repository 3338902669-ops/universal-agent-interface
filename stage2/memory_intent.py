"""UAI MemoryIntent：团队记忆的结构化契约。

为什么不是自由文本：自由文本灌图会把实体抽取弄脏，且无法机械校验
「这条到底算决定、里程碑还是失败」。本模块只做三件事——校验、归一、
编译成 Graphiti episode payload。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Iterator

VALID_KINDS = ("decision", "milestone", "failure", "blockage", "handoff", "verdict")
VALID_EVIDENCE_LEVELS = ("E1", "E2", "E3", "E4")
VALID_SENSITIVITY = ("public", "internal")

MAX_CLAIM_CHARS = 1200
MAX_SUBJECT_CHARS = 200
MAX_EVIDENCE_CHARS = 600

DEFAULT_GROUP = "team"
GROUP_PATTERN = re.compile(r"^[a-z0-9_-]{1,32}$")
_WS = re.compile(r"[ \t]{2,}")

# 凭据识别用正则而不是子串：既要抓住 "my token is abc123" 这种自然语句，
# 又要把 "secret sauce" 这类正常用词放过去。旧实现是 5 个裸子串，两个方向都错。
CREDENTIAL_PATTERNS = (
    r"api[ _-]?key",
    r"apikey",
    r"passwd",
    r"password",
    r"secret[ _-]?key",
    r"client[ _-]?secret",
    r"secret\s*[:=]",
    r"(?:access|refresh|id)[ _-]?token",
    r"token\s*(?:is|[:=])\s*\S{4,}",
    r"bearer\s+[A-Za-z0-9._\-]{8,}",
    r"-----begin [a-z0-9 ]*private key",
    r"\bsk-[A-Za-z0-9]{8,}",
    r"\bghp_[A-Za-z0-9]{8,}",
    r"\bxox[baprs]-",
    r"\bAKIA[0-9A-Z]{12,}",
)
_CREDENTIAL_RE = re.compile("|".join(CREDENTIAL_PATTERNS), re.IGNORECASE)

# 兼容旧引用（team_memory 自己的标记表仍是独立的，这里是记忆管道的口径）
CREDENTIAL_MARKERS = tuple(CREDENTIAL_PATTERNS)


def looks_like_credential(text: str) -> bool:
    return bool(_CREDENTIAL_RE.search(text or ""))


def _iter_strings(value: Any) -> Iterator[str]:
    """递归取出所有字符串叶子：refs/session/task_id 都不能成为凭据后门。

    用显式栈而不是深度截断：旧实现 `depth > 5` 直接 return，深层 refs
    里的凭据会一路入队落盘。同时用 id() 记已访问容器，防自引用死循环。
    """
    stack: list[Any] = [value]
    seen: set[int] = set()
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            if id(item) in seen:
                continue
            seen.add(id(item))
            for key, sub in item.items():
                yield str(key)
                stack.append(sub)
        elif isinstance(item, (list, tuple, set, frozenset)):
            if id(item) in seen:
                continue
            seen.add(id(item))
            stack.extend(item)


def _as_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _collapse(text: str) -> str:
    """折叠行内多余空格，但保留换行（body 允许多行）。"""
    lines = [(_WS.sub(" ", line)).strip() for line in (text or "").splitlines()]
    return "\n".join(line for line in lines if line)


def validate_intent(intent: Any) -> str | None:
    """返回拒绝原因，合法返回 None。原因串以 invalid_intent: 开头。"""
    if not isinstance(intent, dict):
        return "invalid_intent:not_object"
    if not _as_text(intent.get("intent_id")):
        return "invalid_intent:missing_intent_id"
    if _as_text(intent.get("kind")) not in VALID_KINDS:
        return "invalid_intent:unsupported_kind"
    subject = _as_text(intent.get("subject"))
    if not subject:
        return "invalid_intent:missing_subject"
    if len(subject) > MAX_SUBJECT_CHARS:
        return "invalid_intent:subject_too_long"
    claim = _as_text(intent.get("claim"))
    if not claim:
        return "invalid_intent:missing_claim"
    if len(claim) > MAX_CLAIM_CHARS:
        return "invalid_intent:claim_too_long"
    level = _as_text(intent.get("evidence_level"))
    if level not in VALID_EVIDENCE_LEVELS:
        return "invalid_intent:invalid_evidence_level"
    evidence = _as_text(intent.get("evidence"))
    if len(evidence) > MAX_EVIDENCE_CHARS:
        return "invalid_intent:evidence_too_long"
    if level == "E1" and not evidence:
        return "invalid_intent:e1_requires_evidence"
    if _as_text(intent.get("sensitivity") or "internal") == "secret":
        return "invalid_intent:sensitivity_secret_rejected"
    # 扫全量字符串叶子，而不是只扫三个字段：refs / source_session / task_id 同样能藏密钥
    for text in _iter_strings(intent):
        if looks_like_credential(text):
            return "invalid_intent:credential_like_text"
    group = _as_text(intent.get("group_id") or DEFAULT_GROUP)
    if not GROUP_PATTERN.match(group):
        return "invalid_intent:invalid_group_id"
    ref = _as_text(intent.get("reference_time"))
    if not ref:
        return "invalid_intent:invalid_reference_time"
    try:
        datetime.fromisoformat(ref.replace("Z", "+00:00"))
    except ValueError:
        return "invalid_intent:invalid_reference_time"
    if not isinstance(intent.get("refs") or {}, dict):
        return "invalid_intent:invalid_refs"
    return None


def normalize_intent(intent: dict[str, Any]) -> dict[str, Any]:
    """校验并归一；不合法抛 ValueError（带原因串）。"""
    reason = validate_intent(intent)
    if reason:
        raise ValueError(reason)
    refs = dict(intent.get("refs") or {})
    return {
        "intent_id": _as_text(intent["intent_id"]),
        "kind": _as_text(intent["kind"]),
        "subject": _collapse(_as_text(intent["subject"])),
        "claim": _collapse(_as_text(intent["claim"])),
        "evidence_level": _as_text(intent["evidence_level"]),
        "evidence": _collapse(_as_text(intent.get("evidence"))),
        "source_agent": _as_text(intent.get("source_agent") or "unknown") or "unknown",
        "source_session": _as_text(intent.get("source_session")),
        "group_id": _as_text(intent.get("group_id") or DEFAULT_GROUP) or DEFAULT_GROUP,
        "reference_time": _as_text(intent["reference_time"]),
        "sensitivity": _as_text(intent.get("sensitivity") or "internal") or "internal",
        "task_id": _as_text(intent.get("task_id")) or None,
        "refs": refs,
    }


def intent_key(intent_id: str) -> str:
    return "uai|%s" % intent_id


def build_episode_payload(intent: dict[str, Any]) -> dict[str, Any]:
    """编译成 Graphiti /episodes 的 payload（含幂等键与可回读的 name）。"""
    norm = normalize_intent(intent)
    body_lines = ["%s：%s" % (norm["subject"], norm["claim"])]
    body_lines.append("证据等级：%s" % norm["evidence_level"])
    if norm["evidence"]:
        body_lines.append("证据：%s" % norm["evidence"])
    body_lines.append("来源：%s" % norm["source_agent"])
    if norm["source_session"]:
        body_lines.append("会话：%s" % norm["source_session"])
    if norm["task_id"]:
        body_lines.append("任务：%s" % norm["task_id"])
    for key, value in sorted(norm["refs"].items()):
        body_lines.append("%s：%s" % (key, value))
    return {
        "intent_id": norm["intent_id"],
        "intent_key": intent_key(norm["intent_id"]),
        "name": "uai:%s" % norm["intent_id"],
        "body": "\n".join(body_lines),
        "group_id": norm["group_id"],
        "reference_time": norm["reference_time"],
        "source_description": "uai-memory/%s" % norm["source_agent"],
        "kind": norm["kind"],
        "task_id": norm["task_id"],
    }
