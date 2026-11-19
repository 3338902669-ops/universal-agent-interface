"""UAI v1 派单任务契约：在 hub 接单前验证，在运行时转成单行 prompt。"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

MAX_ALLOWED_PATHS = 16
MAX_FORBIDDEN_ACTIONS = 16


def validate_task_contract(envelope: dict[str, Any]) -> str | None:
    """返回稳定的拒绝原因；无 task_contract 时兼容既有 v0.2/v1 派单。"""
    params = envelope.get("params") or {}
    if not isinstance(params, dict):
        return "invalid_params"
    contract = params.get("task_contract")
    if contract is None:
        return None
    if not isinstance(contract, dict):
        return "invalid_task_contract:not_object"
    if contract.get("version") != 1:
        return "invalid_task_contract:unsupported_version"

    scope = contract.get("scope")
    if not isinstance(scope, str) or not scope.strip() or "\n" in scope or "\r" in scope:
        return "invalid_task_contract:invalid_scope"
    instruction = envelope.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip() or "\n" in instruction or "\r" in instruction:
        return "invalid_task_contract:instruction_must_be_single_line"

    paths = contract.get("allowed_paths")
    if not isinstance(paths, list) or not paths or len(paths) > MAX_ALLOWED_PATHS:
        return "invalid_task_contract:invalid_allowed_paths"
    for raw_path in paths:
        if not isinstance(raw_path, str) or not raw_path:
            return "invalid_task_contract:invalid_allowed_path"
        path = Path(raw_path)
        if not path.is_absolute():
            return "invalid_task_contract:allowed_path_not_absolute"
        if not path.is_file():
            return "invalid_task_contract:allowed_path_missing"

    forbidden = contract.get("forbidden_actions")
    if not isinstance(forbidden, list) or len(forbidden) > MAX_FORBIDDEN_ACTIONS:
        return "invalid_task_contract:invalid_forbidden_actions"
    if any(not isinstance(item, str) or not item.strip() or "\n" in item or "\r" in item for item in forbidden):
        return "invalid_task_contract:invalid_forbidden_action"

    response_format = contract.get("response_format")
    if not isinstance(response_format, str) or not response_format.strip() or "\n" in response_format or "\r" in response_format:
        return "invalid_task_contract:invalid_response_format"
    budget = contract.get("time_limit_s")
    if (
        not isinstance(budget, (int, float))
        or isinstance(budget, bool)
        or not math.isfinite(budget)
        or budget <= 0
    ):
        return "invalid_task_contract:invalid_time_limit"
    if budget > float(envelope.get("timeout_s", 0)):
        return "invalid_task_contract:time_limit_exceeds_envelope"
    return None


def build_runtime_instruction(envelope: dict[str, Any], preamble: str) -> str:
    """把结构化任务契约编译成所有 adapter 可安全接收的一行 prompt。"""
    contract = (envelope.get("params") or {}).get("task_contract")
    instruction = str(envelope["instruction"]).strip()
    if not contract:
        return _single_line(instruction + " " + preamble)

    allowed = "; ".join(str(Path(item).resolve()) for item in contract["allowed_paths"])
    forbidden = "；".join(item.strip() for item in contract["forbidden_actions"]) or "无"
    compiled = (
        instruction
        + " 【UAI 任务契约】范围：" + contract["scope"].strip()
        + "。允许读取的绝对路径：" + allowed
        + "。禁止：" + forbidden
        + "。运行时限：" + str(contract["time_limit_s"]) + " 秒"
        + "。回执格式：" + contract["response_format"].strip()
        + "。" + preamble
    )
    return _single_line(compiled)


def _single_line(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())
