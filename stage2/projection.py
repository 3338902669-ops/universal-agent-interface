"""Stage 2 边界投影层：本契约内部对象 → A2A 官方对象。

依据：
  - 契约 v1：`protocol/contract-v1.md`（状态机 §4、事件 §5、裁定 §10）
  - A2A 官方：`a2aproject/A2A` `specification/a2a.proto`（sha16 945DF6E34001B2BF）
  - 映射规格：`~/.shared/A2A-SPEC-MAPPING.md` v1.4

三条不变量（映射表 §7.1）：
  1. 内部账本是唯一事实源；本模块只做只读派生，不写账本。
  2. 投影幂等：同一输入必得同一输出。
  3. 投影失败不得影响控制面。

红线（映射表 §4.3 / §5 / §6）：
  - `SUCCEEDED` ≠ 通过。投影为 COMPLETED 时必须带裁定或明文「待核验」。
  - A2A `REJECTED` 是"拒绝受理"，与本契约「不通过」无关，禁止互映。
  - `heartbeat` 不对外投影。
  - `驳回` 不得出现在任何产出路径（只读别名，写时拒绝）。
"""

from __future__ import annotations

CANONICAL_VERDICTS = ("通过", "带注通过", "条件通过", "不通过", "未核", "作废")
LEGACY_ALIASES = {"驳回": "不通过"}
PENDING_VERIFICATION_TEXT = "待核验"

# 本契约八态 → A2A TaskState（proto 枚举名）。DISPATCHED/CLAIMED 合并投影。
STATE_TO_A2A = {
    "QUEUED": "TASK_STATE_SUBMITTED",
    "DISPATCHED": "TASK_STATE_SUBMITTED",
    "CLAIMED": "TASK_STATE_WORKING",
    "RUNNING": "TASK_STATE_WORKING",
    "SUCCEEDED": "TASK_STATE_COMPLETED",
    "FAILED": "TASK_STATE_FAILED",
    "TIMED_OUT": "TASK_STATE_FAILED",
    "CANCELLED": "TASK_STATE_CANCELED",
}

# 本契约八种事件 → 是否对外投影。heartbeat 明确不投影。
EVENT_PROJECTION = {
    "accepted": "task_object",
    "claimed": "status_update",
    "progress": "status_update",
    "heartbeat": None,          # 不对外
    "result_ready": "artifact_update",
    "completed": "status_update",
    "failed": "status_update",
    "cancelled": "status_update",
}

# TIMED_OUT 与 FAILED 都映到 FAILED，靠原因码区分
FAILURE_REASON_CODE = {
    "FAILED": "failed",
    "TIMED_OUT": "timed_out",
}


class ProjectionError(ValueError):
    """投影无法进行（输入不满足契约）。"""


def canonicalize_verdict(value: str) -> str:
    """读路径归一：历史值 → canonical。写路径请用 assert_writable_verdict。"""
    if value in LEGACY_ALIASES:
        return LEGACY_ALIASES[value]
    return value


def assert_writable_verdict(value: str) -> str:
    """写路径：只接受 canonical 六值；历史别名与未知值一律拒绝。"""
    if value in LEGACY_ALIASES:
        raise ProjectionError(
            "legacy_verdict_not_writable: %r 是只读别名，写入须用 %r"
            % (value, LEGACY_ALIASES[value])
        )
    if value not in CANONICAL_VERDICTS:
        raise ProjectionError("unknown_verdict: %r" % (value,))
    return value


def state_of(internal_state: str) -> str:
    """本契约八态 → A2A TaskState 枚举名。非八态直接拒绝。"""
    try:
        return STATE_TO_A2A[internal_state]
    except KeyError:
        raise ProjectionError("unknown_internal_state: %r" % (internal_state,))


def project_status(
    internal_state: str,
    *,
    verdict: str | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """内部状态 → A2A TaskStatus。

    SUCCEEDED 时：有裁定则带 canonical 裁定，无裁定则 message 明文「待核验」。
    TIMED_OUT/FAILED 时：原因码进 message。
    """
    a2a_state = state_of(internal_state)
    message_text = None

    if internal_state == "SUCCEEDED":
        if verdict is None:
            message_text = PENDING_VERIFICATION_TEXT
        else:
            message_text = assert_writable_verdict(verdict)
    elif internal_state in FAILURE_REASON_CODE:
        message_text = reason or FAILURE_REASON_CODE[internal_state]
    elif internal_state == "CANCELLED":
        message_text = reason or "cancelled"

    status = {"state": a2a_state}
    if message_text is not None:
        status["message"] = {"parts": [{"text": message_text}]}
    if timestamp:
        status["timestamp"] = timestamp
    # 精度补偿：DISPATCHED/CLAIMED 合并后靠 metadata 保留原态
    status["metadata"] = {"uai/state": internal_state}
    return status


def project_task(
    *,
    task_id: str,
    internal_state: str,
    context_id: str | None = None,
    instruction: str | None = None,
    verdict: str | None = None,
    reason: str | None = None,
    timestamp: str | None = None,
) -> dict:
    """内部任务 → A2A Task 对象。"""
    if not task_id:
        raise ProjectionError("missing_task_id")
    task = {
        "id": task_id,
        "status": project_status(
            internal_state, verdict=verdict, reason=reason, timestamp=timestamp
        ),
    }
    if context_id:
        task["context_id"] = context_id
    if instruction:
        task["history"] = [
            {"role": "user", "parts": [{"text": instruction}]}
        ]
    return task


def project_event(event: dict) -> dict | None:
    """内部事件 → A2A 事件对象。heartbeat 返回 None（不投影）。"""
    kind = event.get("kind")
    if kind not in EVENT_PROJECTION:
        raise ProjectionError("unknown_event_kind: %r" % (kind,))
    target = EVENT_PROJECTION[kind]
    if target is None:
        return None

    task_id = event.get("task_id")
    if not task_id:
        raise ProjectionError("missing_task_id")

    if target == "task_object":
        return {
            "kind": "task",
            "task": project_task(
                task_id=task_id,
                internal_state="QUEUED",
                context_id=event.get("context_id"),
                timestamp=event.get("occurred_at"),
            ),
        }

    if target == "status_update":
        internal_state = {
            "claimed": "CLAIMED",
            "progress": "RUNNING",
            "completed": "SUCCEEDED",
            "failed": "FAILED",
            "cancelled": "CANCELLED",
        }[kind]
        return {
            "kind": "status_update",
            "task_id": task_id,
            "context_id": event.get("context_id"),
            "status": project_status(
                internal_state,
                verdict=event.get("verdict"),
                reason=event.get("reason_code"),
                timestamp=event.get("occurred_at"),
            ),
            "final": internal_state in ("SUCCEEDED", "FAILED", "CANCELLED"),
        }

    # artifact_update
    ref = event.get("result_ref") or {}
    if not ref.get("uri"):
        raise ProjectionError("result_ready_without_result_ref")
    return {
        "kind": "artifact_update",
        "task_id": task_id,
        "context_id": event.get("context_id"),
        "artifact": {
            "artifact_id": ref.get("uri") or (task_id + ":result"),
            "parts": [
                {
                    "url": ref.get("uri"),
                    "media_type": ref.get("media_type") or "application/octet-stream",
                }
            ],
            # Part 没有 sha256 字段，指纹只能进 metadata
            "metadata": {
                "uai/sha256": ref.get("sha256"),
                "uai/bytes": ref.get("bytes"),
            },
        },
        "last_chunk": True,
    }


def project_agent_card(
    *,
    interface_id: str,
    contract_version: str,
    control_subject: str,
    capabilities: list[dict],
    public_url: str,
    protocol_binding: str = "NATS",
    description: str | None = None,
) -> dict:
    """内部名片 → A2A AgentCard。

    AgentCard 没有顶层 url；地址与绑定方式进 supported_interfaces[]。
    control_subject 是内网 subject，**不得**原样对外；对外只发 public_url。
    """
    if not interface_id:
        raise ProjectionError("missing_interface_id")
    if not capabilities:
        raise ProjectionError("missing_capabilities")
    if not public_url.startswith(("http://", "https://")):
        raise ProjectionError("public_url_must_be_absolute_http: %r" % (public_url,))
    if control_subject and control_subject in public_url:
        raise ProjectionError("public_url_leaks_internal_subject")

    skills = []
    for cap in capabilities:
        skills.append(
            {
                "id": cap["name"],
                "name": cap.get("name"),
                "description": cap.get("description", ""),
                "tags": [cap["duration_class"]] if cap.get("duration_class") else [],
                "input_modes": [cap.get("input_type")] if cap.get("input_type") else [],
                "output_modes": [cap.get("output_type")] if cap.get("output_type") else [],
            }
        )

    return {
        "name": interface_id,
        "description": description or interface_id,
        "version": contract_version,
        "supported_interfaces": [
            {
                "url": public_url,
                "protocol_binding": protocol_binding,
                "protocol_version": contract_version,
            }
        ],
        "capabilities": {"streaming": True, "push_notifications": True},
        "default_input_modes": ["text/plain"],
        "default_output_modes": ["application/json"],
        "skills": skills,
    }
