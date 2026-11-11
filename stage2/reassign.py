"""任务改派（A-11）：把任务从一个席位交给另一个席位，并用更大围栏作废旧持有者。

待完善项 A-11 的根因（GAR 的 F-GAR-02）：治理层没有 reassign 操作，
`claimTask` 要求 `assignedAgent === agent`，`recoverTask` 只清锁不改 assignee，
所以跨 Agent 交接只能表达成"同 Agent 递增 epoch"。

本模块给的是**最小可验证语义**：

  1. 改派必须签发**更大的 fence**（+1）与**新的 attempt**；
  2. 旧持有者的迟到结果按 `fencing_stale` 拒绝（不是静默丢弃，要留审计）；
  3. 改派必须写原因（对应 AO 的 C3：Recovery MUST be explicit and MUST state a reason）；
  4. 不允许把 `governance_state=blocked` 的任务改派出去（对应 C2）。
"""

from __future__ import annotations

from dataclasses import dataclass


class ReassignError(ValueError):
    pass


TERMINAL_EXECUTION = ("SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED")


@dataclass
class Lease:
    """中枢对一个任务签发的租约。"""
    task_id: str
    interface_id: str
    attempt: int = 1
    fence: int = 1
    expires_at: str | None = None

    def as_record(self) -> dict:
        return {
            "task_id": self.task_id,
            "interface_id": self.interface_id,
            "attempt": self.attempt,
            "fence": self.fence,
            "lease_expires_at": self.expires_at,
        }


def reassign(
    lease: Lease,
    *,
    to_interface: str,
    reason: str,
    execution_state: str = "CLAIMED",
    governance_state: str = "active",
    now: str | None = None,
) -> tuple[Lease, dict]:
    """改派：返回新租约与一条可入账的事件。"""
    if not reason or not str(reason).strip():
        raise ReassignError("reassign_requires_reason")          # C3
    if not to_interface:
        raise ReassignError("reassign_requires_target")
    if to_interface == lease.interface_id:
        raise ReassignError("reassign_target_is_current_holder")
    if governance_state == "blocked":
        raise ReassignError("blocked_task_must_not_be_reassigned")  # C2
    if execution_state in TERMINAL_EXECUTION:
        raise ReassignError("terminal_task_must_not_be_reassigned")

    new = Lease(
        task_id=lease.task_id,
        interface_id=to_interface,
        attempt=lease.attempt + 1,
        fence=lease.fence + 1,                 # 规则 1：围栏必须增大
        expires_at=now,
    )
    event = {
        "record": "TASK_REASSIGNED",
        "task_id": lease.task_id,
        "from_interface": lease.interface_id,
        "to_interface": to_interface,
        "revoked_fencing_generation": lease.fence,
        "fencing_generation": new.fence,
        "attempt": new.attempt,
        "reason": str(reason),
        "ts": now,
    }
    return new, event


def accept_result(lease: Lease, *, fence: int, attempt: int) -> dict:
    """按当前租约校验写入。旧围栏一律拒绝并留审计原因（规则 2）。"""
    if fence < lease.fence:
        return {"accepted": False, "reason": "fencing_stale",
                "expected_fence": lease.fence, "got_fence": fence}
    if fence > lease.fence:
        return {"accepted": False, "reason": "fencing_ahead",
                "expected_fence": lease.fence, "got_fence": fence}
    if attempt != lease.attempt:
        return {"accepted": False, "reason": "stale_attempt",
                "expected_attempt": lease.attempt, "got_attempt": attempt}
    return {"accepted": True, "reason": None}
