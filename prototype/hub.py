#!/usr/bin/env python3
"""Append-only control-plane hub for the v0.2 interface prototype."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import nats
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from core import (
    CONTRACT_VERSION,
    SUPPORTED_CONTRACT_VERSIONS,
    VALID_EVENT_KINDS,
    AppendOnlyJsonl,
    append_log,
    canonical_json,
    envelope_digest,
    file_uri_to_path,
    now_iso,
    sha256_file,
    sign_value,
    token_digest,
    verify_proof,
    verify_value,
)


TERMINAL_STATES = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}
ACTIVE_EXECUTION_STATES = {"DISPATCHED", "CLAIMED", "RUNNING"}
VERDICT_WORDS = {"通过", "带注通过", "条件通过", "不通过", "未核", "作废"}


def iso_after(seconds: float) -> str:
    return (
        datetime.now(timezone.utc) + timedelta(seconds=seconds)
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _load_memory_pipeline():
    """按需加载 stage2 记忆管道（hub 不硬依赖它，缺了也要能跑）。"""
    import sys as _sys

    stage2 = Path(__file__).resolve().parent.parent / "stage2"
    if str(stage2) not in _sys.path:
        _sys.path.insert(0, str(stage2))
    import memory_pipeline

    return memory_pipeline


def _graph_writes_enabled() -> bool:
    try:
        import sys as _sys

        stage2 = Path(__file__).resolve().parent.parent / "stage2"
        if str(stage2) not in _sys.path:
            _sys.path.insert(0, str(stage2))
        from outcome_recorder import graph_writes_enabled

        return graph_writes_enabled()
    except Exception:
        return False


@dataclass
class InterfaceSession:
    interface_id: str
    token: str | None = None
    card: dict[str, Any] | None = None
    control_subject: str = ""
    heartbeat_s: float = 10.0
    last_heartbeat: float = 0.0
    active: bool = False
    stale: bool = False
    stale_task: asyncio.Task[Any] | None = None


@dataclass
class Task:
    task_id: str
    envelope: dict[str, Any]
    trace_id: str
    idempotency_key: str
    state: str = "QUEUED"
    selected_interface: str | None = None
    dispatch_id: str | None = None
    fencing_generation: int = 0
    lease_expires_at: float | None = None
    event_seq: int = 0
    accepted_at: str = ""
    claimed_at: str = ""
    updated_at: str = ""
    result_pointer: dict[str, Any] | None = None
    verdict: dict[str, Any] | None = None
    lease_timer: asyncio.Task[Any] | None = None
    timeout_timer: asyncio.Task[Any] | None = None
    exclude_once: str | None = None
    dispatch_attempts: int = 0
    terminal_reason: str | None = None
    blocked_interfaces: set[str] = field(default_factory=set)


class Hub:
    def __init__(self, base: Path, nats_url: str, lease_seconds: float = 300.0):
        self.base = base
        self.nats_url = nats_url
        self.default_lease_seconds = lease_seconds
        # UAI-08：裁定写回图谱的超时（秒）。图谱慢/挂不能拖住裁定。
        self.graph_memory_timeout_s = float(os.environ.get("UAI_GRAPH_MEMORY_TIMEOUT", "3.0"))
        # outbox 自愈周期：0 = 关闭（测试/离线场景）。落盘不丢，只是晚一点写进图。
        self.memory_drain_s = float(os.environ.get("UAI_MEMORY_DRAIN_S", "60"))
        # 持有后台记忆任务的强引用：否则 create_task 的返回值被 GC，任务可能被回收
        self._memory_tasks: set[asyncio.Task[Any]] = set()
        self.ledger_path = base / "data" / "ledger.jsonl"
        self.ledger = AppendOnlyJsonl(self.ledger_path)
        # 心跳是连接保活，不是审计凭证：单独落盘，不进哈希链主账本
        self.heartbeat_path = base / "data" / "heartbeats.jsonl"
        self.heartbeat_log = AppendOnlyJsonl(self.heartbeat_path)
        from ledger_seal import LedgerSealer
        self.sealer = LedgerSealer(self.ledger_path, base / "data" / "chain-heads.jsonl")
        self.connection_log = base / "logs" / "hub-connection.jsonl"
        self.token_hashes_path = base / "data" / "token-hashes.json"
        self.nc: NATS | None = None
        self.sessions: dict[str, InterfaceSession] = {}
        self.tasks: dict[str, Task] = {}
        self.idempotency: dict[str, str] = {}
        self.dispatch_lock = asyncio.Lock()
        self.dispatch_scheduled = False
        self.events_paused_until = 0.0
        self.stop_event = asyncio.Event()
        self._replay()

    def _replay(self) -> None:
        """Reconstruct append-only facts. Registration/auth is intentionally not restored."""

        for record in self.ledger.records():
            kind = record.get("record")
            if kind == "TASK_ACCEPTED":
                envelope = record["envelope"]
                task = Task(
                    task_id=envelope["task_id"],
                    envelope=envelope,
                    trace_id=envelope["trace_id"],
                    idempotency_key=envelope["idempotency_key"],
                    accepted_at=record["ts"],
                    updated_at=record["ts"],
                )
                self.tasks[task.task_id] = task
                self.idempotency[task.idempotency_key] = task.task_id
            elif kind == "TASK_STATE" and record.get("task_id") in self.tasks:
                task = self.tasks[record["task_id"]]
                task.state = record["state"]
                task.updated_at = record["ts"]
                task.selected_interface = record.get("interface_id") or task.selected_interface
                task.dispatch_id = record.get("dispatch_id") or task.dispatch_id
                task.fencing_generation = record.get(
                    "fencing_generation", task.fencing_generation
                )
                task.event_seq = record.get("event_seq", task.event_seq)
                task.result_pointer = record.get("result_pointer", task.result_pointer)
                task.terminal_reason = record.get("reason", task.terminal_reason)
            elif kind == "TASK_EVENT" and record.get("task_id") in self.tasks:
                task = self.tasks[record["task_id"]]
                task.event_seq = max(task.event_seq, int(record.get("seq", 0)))
            elif kind == "VERDICT" and record.get("task_id") in self.tasks:
                self.tasks[record["task_id"]].verdict = record

    def record(self, record_type: str, **fields: Any) -> dict[str, Any]:
        value = {"record": record_type, "ts": now_iso(), **fields}
        # 哈希链接线（2026-11-15）：写前 seal，篡改/删链可检出；历史未链化记录渐进兼容
        self.ledger.append(self.sealer.seal(value))
        return value

    def record_heartbeat(self, interface_id: str) -> None:
        """心跳只写独立日志：不封链、不进主账本。
        主账本要留的是状态变更（注册/失联/裁定）与业务事实。
        """
        self.heartbeat_log.append({"ts": now_iso(), "interface_id": interface_id})

    def load_token_hashes(self) -> dict[str, str]:
        if not self.token_hashes_path.exists():
            return {}
        with self.token_hashes_path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
        return {str(k): str(v) for k, v in value.items()}

    def json_response(self, msg: Msg, value: dict[str, Any]) -> None:
        if msg.reply:
            asyncio.create_task(
                msg.respond(
                    json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
                        "utf-8"
                    )
                )
            )

    async def connect(self) -> None:
        async def disconnected_cb() -> None:
            append_log(
                self.connection_log,
                "hub",
                "connection_down",
                nats_url=self.nats_url,
            )

        async def reconnected_cb() -> None:
            append_log(
                self.connection_log,
                "hub",
                "connection_up",
                nats_url=self.nats_url,
                server=getattr(self.nc, "connected_url", None)
                if self.nc
                else None,
            )
            self.schedule_dispatch()

        async def error_cb(error: Exception) -> None:
            append_log(
                self.connection_log,
                "hub",
                "connection_error",
                error_type=type(error).__name__,
                error=str(error)[:300],
            )

        async def closed_cb() -> None:
            append_log(self.connection_log, "hub", "connection_closed")

        self.nc = await nats.connect(
            self.nats_url,
            name="interface-prototype-hub",
            connect_timeout=3,
            reconnect_time_wait=1,
            max_reconnect_attempts=-1,
            allow_reconnect=True,
            disconnected_cb=disconnected_cb,
            reconnected_cb=reconnected_cb,
            error_cb=error_cb,
            closed_cb=closed_cb,
        )
        await self.nc.subscribe("hub.register", cb=self.on_register)
        await self.nc.subscribe("hub.heartbeat", cb=self.on_heartbeat)
        await self.nc.subscribe("hub.claim", cb=self.on_claim)
        await self.nc.subscribe("hub.event", cb=self.on_event)
        await self.nc.subscribe("hub.submit", cb=self.on_submit)
        await self.nc.subscribe("hub.state", cb=self.on_state)
        await self.nc.subscribe("hub.verdict", cb=self.on_verdict)
        await self.nc.subscribe("hub.debug", cb=self.on_debug)
        await self.nc.subscribe("hub.reconcile", cb=self.on_reconcile)
        append_log(
            self.connection_log,
            "hub",
            "connection_up",
            nats_url=self.nats_url,
            server=getattr(self.nc, "connected_url", None),
        )
        self.record("HUB_START", contract_version=CONTRACT_VERSION, pid=os.getpid())
        if self.memory_drain_s > 0:
            asyncio.create_task(self._memory_drain_loop())

    @staticmethod
    def _decode(msg: Msg) -> dict[str, Any] | None:
        try:
            value = json.loads(msg.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _register_rejection(self, interface_id: str, reason: str) -> None:
        self.record(
            "REGISTRATION_REJECTED",
            interface_id=interface_id,
            reason=reason,
        )

    async def on_register(self, msg: Msg) -> None:
        request = self._decode(msg)
        if not request:
            self._register_rejection("unknown", "malformed_registration")
            self.json_response(msg, {"ok": False, "reason": "malformed_registration"})
            return
        interface_id = str(request.get("interface_id", ""))
        card = request.get("capability_card")
        token = request.get("token_secret")
        if not interface_id or not isinstance(card, dict) or not isinstance(token, str):
            self._register_rejection(interface_id or "unknown", "invalid_registration")
            self.json_response(msg, {"ok": False, "reason": "invalid_registration"})
            return
        hashes = self.load_token_hashes()
        if token_digest(token) != hashes.get(interface_id):
            self._register_rejection(interface_id, "invalid_token")
            self.json_response(msg, {"ok": False, "reason": "invalid_token"})
            return
        try:
            heartbeat_s = max(0.5, float(card["heartbeat_s"]))
            control_subject = str(card["transport"]["control_subject"])
            if card["interface_id"] != interface_id:
                raise ValueError("interface_id mismatch")
            if card["contract_version"] not in SUPPORTED_CONTRACT_VERSIONS:
                raise ValueError("contract version mismatch")
            capabilities = card["capabilities"]
            if not isinstance(capabilities, list) or not capabilities:
                raise ValueError("missing capabilities")
            sandbox = card["sandbox"]
            if not sandbox.get("isolated") and not sandbox.get("code_execution_allowed", True):
                raise ValueError("unsafe sandbox declaration")
        except (KeyError, TypeError, ValueError) as error:
            self._register_rejection(interface_id, f"invalid_capability_card:{error}")
            self.json_response(
                msg, {"ok": False, "reason": "invalid_capability_card"}
            )
            return

        previous = self.sessions.get(interface_id)
        if previous and previous.stale_task:
            previous.stale_task.cancel()
        session = InterfaceSession(
            interface_id=interface_id,
            token=token,
            card=card,
            control_subject=control_subject,
            heartbeat_s=heartbeat_s,
            last_heartbeat=time.monotonic(),
        )
        self.sessions[interface_id] = session
        self.record(
            "REGISTRATION_PENDING",
            interface_id=interface_id,
            capability_count=len(capabilities),
            heartbeat_s=heartbeat_s,
        )

        probe_nonce = uuid.uuid4().hex
        probe_request = {
            "kind": "probe",
            "interface_id": interface_id,
            "nonce": probe_nonce,
        }
        try:
            probe_response = await self.nc.request(
                control_subject,
                canonical_json(probe_request),
                timeout=max(1.0, min(4.0, heartbeat_s * 2)),
            )
            probe = json.loads(probe_response.data.decode("utf-8"))
            probe_clean = dict(probe)
            proof = probe_clean.pop("proof", None)
            if (
                probe.get("kind") != "probe_ack"
                or probe.get("interface_id") != interface_id
                or probe.get("nonce") != probe_nonce
                or not isinstance(proof, str)
                or not verify_value(token, probe_clean, proof)
            ):
                raise ValueError("probe_auth_failed")
        except Exception as error:
            reason = f"probe_failed:{type(error).__name__}"
            self.record(
                "ACCESS_CHECK_FAILED",
                interface_id=interface_id,
                step="probe",
                reason=reason,
            )
            self._register_rejection(interface_id, reason)
            self.json_response(msg, {"ok": False, "reason": reason})
            return
        self.record("ACCESS_CHECK_OK", interface_id=interface_id, step="probe")

        marker = (
            f"v0.2|{uuid.uuid4().hex}|引号\"单引号'反斜杠\\换行\n尾部"
        )
        challenge_id = uuid.uuid4().hex
        echo_request = {
            "kind": "echo",
            "interface_id": interface_id,
            "challenge_id": challenge_id,
            "marker": marker,
        }
        try:
            echo_response = await self.nc.request(
                control_subject,
                canonical_json(echo_request),
                timeout=max(1.0, min(4.0, heartbeat_s * 2)),
            )
            echo = json.loads(echo_response.data.decode("utf-8"))
            echo_clean = dict(echo)
            echo_proof = echo_clean.pop("proof", None)
            if (
                echo.get("kind") != "echo_ack"
                or echo.get("interface_id") != interface_id
                or echo.get("challenge_id") != challenge_id
                or echo.get("marker") != marker
                or not isinstance(echo_proof, str)
                or not verify_value(token, echo_clean, echo_proof)
            ):
                raise ValueError("echo_or_auth_mismatch")
        except Exception as error:
            reason = f"echo_failed:{type(error).__name__}"
            self.record(
                "ACCESS_CHECK_FAILED",
                interface_id=interface_id,
                step="echo",
                reason=reason,
            )
            self._register_rejection(interface_id, reason)
            self.json_response(msg, {"ok": False, "reason": reason})
            return
        self.record("ACCESS_CHECK_OK", interface_id=interface_id, step="echo")

        session.active = True
        session.stale = False
        session.last_heartbeat = time.monotonic()
        session.token = token
        self.record(
            "INTERFACE_ACTIVE",
            interface_id=interface_id,
            heartbeat_s=heartbeat_s,
            capability_names=[
                capability.get("name")
                for capability in capabilities
                if isinstance(capability, dict)
            ],
        )
        self._schedule_stale(session)
        self.json_response(
            msg,
            {
                "ok": True,
                "interface_id": interface_id,
                "access_check": ["probe_ok", "echo_ok"],
            },
        )
        self.schedule_dispatch()

    def _schedule_stale(self, session: InterfaceSession) -> None:
        if session.stale_task:
            session.stale_task.cancel()
        session.stale_task = asyncio.create_task(self._stale_after(session))

    async def _stale_after(self, session: InterfaceSession) -> None:
        try:
            while session.active and not session.stale:
                deadline = session.last_heartbeat + 3 * session.heartbeat_s
                wait_s = max(0.05, deadline - time.monotonic())
                await asyncio.sleep(wait_s)
                observed = session.last_heartbeat
                await asyncio.sleep(0.05)
                if session.last_heartbeat != observed:
                    continue
                if (
                    session.active
                    and not session.stale
                    and time.monotonic() >= observed + 3 * session.heartbeat_s
                ):
                    session.stale = True
                    session.active = False
                    self.record(
                        "INTERFACE_STALE",
                        interface_id=session.interface_id,
                        missed_heartbeats=3,
                    )
                    return
        except asyncio.CancelledError:
            return

    async def on_heartbeat(self, msg: Msg) -> None:
        request = self._decode(msg)
        interface_id = str((request or {}).get("interface_id", ""))
        session = self.sessions.get(interface_id)
        if not request:
            self.json_response(msg, {"ok": False, "reason": "malformed"})
            return
        if not session:
            # 中枢重启后席位还在跑：它们的内存里 registered 仍是 True。
            # 只回 unauthorized 的话席位不会动作（实测 80s 都不回来），必须明确
            # 让它重新注册——这是"中枢单独重启后完全没有可用席位"的根因。
            # 这里不写账本：未知席位每 2s 一次心跳会淹掉账本。
            self.json_response(
                msg,
                {"ok": False, "reason": "unknown_session", "must_register": True},
            )
            return
        if not session.token or not verify_proof(session.token, request):
            self.json_response(msg, {"ok": False, "reason": "unauthorized"})
            return
        session.last_heartbeat = time.monotonic()
        self.record_heartbeat(interface_id)
        if session.stale or not session.active:
            self.json_response(
                msg,
                {"ok": False, "reason": "stale", "must_register": True},
            )
            return
        self._schedule_stale(session)
        self.json_response(msg, {"ok": True, "active": True})

    def _can_route(self, task: Task) -> bool:
        target = task.envelope.get("target", {})
        interface_id = target.get("interface_id")
        capability = target.get("capability")
        for session in self.sessions.values():
            if not session.active or session.stale or not session.card:
                continue
            if interface_id and session.interface_id != interface_id:
                continue
            if capability:
                names = {
                    item.get("name")
                    for item in session.card.get("capabilities", [])
                    if isinstance(item, dict)
                }
                if capability not in names:
                    continue
            return True
        return False

    def _select_interface(self, task: Task) -> InterfaceSession | None:
        target = task.envelope.get("target", {})
        interface_id = target.get("interface_id")
        capability = target.get("capability")
        candidates: list[tuple[int, str, InterfaceSession]] = []
        for session in self.sessions.values():
            if not session.active or session.stale or not session.card:
                continue
            if session.interface_id in task.blocked_interfaces:
                continue
            if interface_id and session.interface_id != interface_id:
                continue
            if capability:
                cap = next(
                    (
                        item
                        for item in session.card.get("capabilities", [])
                        if isinstance(item, dict) and item.get("name") == capability
                    ),
                    None,
                )
                if not cap:
                    continue
            else:
                cap = (session.card.get("capabilities") or [{}])[0]
            load = sum(
                1
                for candidate in self.tasks.values()
                if candidate.selected_interface == session.interface_id
                and candidate.state in ACTIVE_EXECUTION_STATES
            )
            maximum = int(cap.get("max_concurrency", 1))
            if load >= maximum:
                continue
            candidates.append((load, session.interface_id, session))
        if task.blocked_interfaces:
            print(
                json.dumps(
                    {
                        "event": "dispatch_exclusion",
                        "task_id": task.task_id,
                        "excluded": sorted(task.blocked_interfaces),
                        "available": len(candidates),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        if not candidates:
            return None
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]

    def _resource_is_busy(self, task: Task) -> bool:
        resource_key = task.envelope.get("resource_key")
        if not resource_key:
            return False
        return any(
            other.task_id != task.task_id
            and other.envelope.get("resource_key") == resource_key
            and other.state in ACTIVE_EXECUTION_STATES
            for other in self.tasks.values()
        )

    def schedule_dispatch(self) -> None:
        if not self.dispatch_scheduled:
            self.dispatch_scheduled = True
            # 关键：create_task 的异常默认没人收（只在 GC 时打一行 warn）。
            # 一旦 _dispatch_round 抛异常或抛在 dispatch_lock 上，dispatch_scheduled
            # 会永远停在 True → 之后**再也不会派发**，而进程/锁/心跳全都健康。
            # 这就是 2026-11-18 实测到的「hub 假在线、任务永远 QUEUED」。
            scheduled = asyncio.create_task(self._dispatch_round())
            scheduled.add_done_callback(self._on_dispatch_done)

    def _on_dispatch_done(self, finished: asyncio.Task[Any]) -> None:
        # 无条件复位：这条标志位是派发循环的“心跳”，卡住就等于中枢瘫痪
        self.dispatch_scheduled = False
        if finished.cancelled():
            return
        error = finished.exception()
        if error is None:
            return
        try:
            self.record(
                "DISPATCH_LOOP_ERROR",
                error=repr(error)[:300],
                note="派发循环异常，已复位并重排",
            )
        except Exception:
            pass
        # 自愈：下一个事件循环轮次重排一次（有 QUEUED 才会真干活）
        try:
            asyncio.get_running_loop().call_later(1.0, self.schedule_dispatch)
        except RuntimeError:
            pass

    async def _dispatch_round(self) -> None:
        async with self.dispatch_lock:
            retry_after_rejection = False
            try:
                while True:
                    queued = sorted(
                        (
                            task
                            for task in self.tasks.values()
                            if task.state == "QUEUED"
                        ),
                        key=lambda task: (task.accepted_at, task.task_id),
                    )
                    progressed = False
                    for task in queued:
                        if not self._can_route(task):
                            continue
                        if self._resource_is_busy(task):
                            continue
                        session = self._select_interface(task)
                        if not session:
                            continue
                        dispatch_id = uuid.uuid4().hex
                        task.state = "DISPATCHED"
                        task.selected_interface = session.interface_id
                        task.dispatch_id = dispatch_id
                        task.dispatch_attempts += 1
                        task.updated_at = now_iso()
                        self.record(
                            "TASK_STATE",
                            task_id=task.task_id,
                            state="DISPATCHED",
                            trigger="dispatch",
                            operator="hub",
                            interface_id=session.interface_id,
                            dispatch_id=dispatch_id,
                            fencing_generation=task.fencing_generation,
                        )
                        delivered, retry_route = await self._send_dispatch(
                            task, session
                        )
                        if not delivered:
                            task.state = "QUEUED"
                            task.selected_interface = None
                            task.dispatch_id = None
                            task.updated_at = now_iso()
                            self.record(
                                "TASK_STATE",
                                task_id=task.task_id,
                                state="QUEUED",
                                trigger="dispatch_failed",
                                operator="hub",
                                reason="delivery_failed",
                            )
                        else:
                            progressed = True
                        retry_after_rejection = (
                            retry_after_rejection or retry_route
                        )
                    if not progressed:
                        break
            finally:
                self.dispatch_scheduled = False
        if retry_after_rejection:
            await asyncio.sleep(0.5)
            self.schedule_dispatch()

    async def _send_dispatch(
        self, task: Task, session: InterfaceSession, debug: bool = False
    ) -> tuple[bool, bool]:
        if not self.nc or not session.token:
            return False, False
        command = {
            "kind": "dispatch",
            "interface_id": session.interface_id,
            "dispatch_id": task.dispatch_id,
            "envelope": task.envelope,
        }
        command["proof"] = sign_value(session.token, command)
        try:
            reply = await self.nc.request(
                session.control_subject,
                canonical_json(command),
                timeout=3,
            )
            value = json.loads(reply.data.decode("utf-8"))
            if not value.get("ok"):
                self.record(
                    "DISPATCH_REJECTED",
                    task_id=task.task_id,
                    interface_id=session.interface_id,
                    reason=value.get("reason", "unknown"),
                    debug=debug,
                )
                return False, bool(value.get("retryable"))
            self.record(
                "DISPATCH_ACK",
                task_id=task.task_id,
                interface_id=session.interface_id,
                dispatch_id=task.dispatch_id,
                duplicate=bool(value.get("duplicate")),
                debug=debug,
            )
            if value.get("duplicate") and not debug:
                task.blocked_interfaces.add(session.interface_id)
                self.record(
                    "INTERFACE_EXCLUDED_FOR_TASK",
                    task_id=task.task_id,
                    interface_id=session.interface_id,
                    reason="duplicate_dispatch",
                )
                return False, True
            return True, False
        except Exception as error:
            self.record(
                "DISPATCH_REJECTED",
                task_id=task.task_id,
                interface_id=session.interface_id,
                reason=f"transport:{type(error).__name__}",
                debug=debug,
            )
            return False, False

    async def on_submit(self, msg: Msg) -> None:
        from core import validate_envelope

        ok, reason, envelope = validate_envelope(msg.data)
        if not ok or envelope is None:
            task_id = "unknown"
            with contextlib.suppress(Exception):
                task_id = str(json.loads(msg.data.decode("utf-8")).get("task_id"))
            self.record(
                "ENVELOPE_REJECTED",
                task_id=task_id,
                reason=reason or "invalid_envelope",
                envelope_sha256=envelope_digest({}) if envelope is None else None,
            )
            self.json_response(
                msg,
                {"ok": False, "reason": reason, "accepted": False},
            )
            return
        existing_id = self.idempotency.get(envelope["idempotency_key"])
        if existing_id:
            task = self.tasks[existing_id]
            self.json_response(
                msg,
                {
                    "ok": True,
                    "duplicate": True,
                    "task_id": task.task_id,
                    "state": task.state,
                },
            )
            return
        task_id = envelope["task_id"]
        if task_id in self.tasks:
            self.record(
                "ENVELOPE_REJECTED",
                task_id=task_id,
                reason="task_id_already_exists",
            )
            self.json_response(
                msg,
                {"ok": False, "reason": "task_id_already_exists"},
            )
            return
        task = Task(
            task_id=task_id,
            envelope=envelope,
            trace_id=envelope["trace_id"],
            idempotency_key=envelope["idempotency_key"],
            accepted_at=now_iso(),
            updated_at=now_iso(),
        )
        self.tasks[task_id] = task
        self.idempotency[task.idempotency_key] = task_id
        digest = envelope_digest(envelope)
        self.record(
            "TASK_ACCEPTED",
            task_id=task_id,
            envelope=envelope,
            envelope_sha256=digest,
            state="QUEUED",
            created_by=envelope["created_by"],
            trace_id=envelope["trace_id"],
        )
        self.record(
            "TASK_STATE",
            task_id=task_id,
            state="QUEUED",
            trigger="accepted",
            operator="hub",
            envelope_sha256=digest,
        )
        self.json_response(
            msg,
            {
                "ok": True,
                "accepted": True,
                "task_id": task_id,
                "state": "QUEUED",
                "envelope_sha256": digest,
            },
        )
        if envelope.get("cls") == "external":
            params = envelope.get("params") or {}
            if not params.get("approval_ref"):
                await self._terminal_fail(
                    task,
                    "FAILED",
                    "non_retryable",
                    "approval_missing",
                    operator="hub",
                )
                return
        self.schedule_dispatch()

    async def on_claim(self, msg: Msg) -> None:
        request = self._decode(msg)
        interface_id = str((request or {}).get("interface_id", ""))
        task_id = str((request or {}).get("task_id", ""))
        dispatch_id = str((request or {}).get("dispatch_id", ""))
        session = self.sessions.get(interface_id)
        task = self.tasks.get(task_id)
        if (
            not request
            or not session
            or not task
            or not session.token
            or not verify_proof(session.token, request)
        ):
            self.json_response(msg, {"ok": False, "reason": "unauthorized"})
            return
        if not session.active or session.stale:
            self.json_response(msg, {"ok": False, "reason": "interface_not_active"})
            return
        if task.state in {"CLAIMED", "RUNNING"} and task.selected_interface == interface_id:
            self.json_response(
                msg,
                {
                    "ok": True,
                    "duplicate": True,
                    "task_id": task_id,
                    "lease_expires_at": task.lease_expires_at,
                    "fencing_generation": task.fencing_generation,
                    "next_seq": task.event_seq + 1,
                },
            )
            return
        if (
            task.state != "DISPATCHED"
            or task.selected_interface != interface_id
            or task.dispatch_id != dispatch_id
        ):
            self.json_response(
                msg,
                {"ok": False, "reason": "claim_not_offered", "state": task.state},
            )
            return
        if interface_id in task.blocked_interfaces:
            self.json_response(msg, {"ok": False, "reason": "fenced_owner_excluded"})
            return
        lease_seconds = self.default_lease_seconds
        params = task.envelope.get("params") or {}
        if isinstance(params, dict) and "_lease_seconds" in params:
            lease_seconds = float(params["_lease_seconds"])
        task.fencing_generation += 1
        task.claimed_at = now_iso()
        task.updated_at = task.claimed_at
        task.state = "CLAIMED"
        task.lease_expires_at = time.time() + lease_seconds
        self.record(
            "TASK_STATE",
            task_id=task_id,
            state="CLAIMED",
            trigger="claim",
            operator=interface_id,
            interface_id=interface_id,
            dispatch_id=dispatch_id,
            fencing_generation=task.fencing_generation,
            lease_expires_at=iso_after(lease_seconds),
            lease_seconds=lease_seconds,
        )
        if task.lease_timer:
            task.lease_timer.cancel()
        task.lease_timer = asyncio.create_task(
            self._lease_expiry(task, task.fencing_generation)
        )
        if task.timeout_timer:
            task.timeout_timer.cancel()
        task.timeout_timer = asyncio.create_task(
            self._task_timeout(task, task.fencing_generation)
        )
        self.json_response(
            msg,
            {
                "ok": True,
                "task_id": task_id,
                "lease_seconds": lease_seconds,
                "lease_expires_at": iso_after(lease_seconds),
                "fencing_generation": task.fencing_generation,
                "next_seq": task.event_seq + 1,
            },
        )

    async def _lease_expiry(self, task: Task, generation: int) -> None:
        try:
            while task.lease_expires_at is not None:
                wait_s = task.lease_expires_at - time.time()
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                if (
                    task.lease_expires_at is not None
                    and task.lease_expires_at > time.time() + 0.01
                ):
                    continue
                if (
                    task.fencing_generation != generation
                    or task.state not in {"CLAIMED", "RUNNING"}
                ):
                    return
                previous = task.selected_interface
                task.state = "QUEUED"
                task.selected_interface = None
                task.dispatch_id = None
                task.lease_expires_at = None
                task.exclude_once = previous
                if previous:
                    task.blocked_interfaces.add(previous)
                task.updated_at = now_iso()
                self.record(
                    "LEASE_EXPIRED",
                    task_id=task.task_id,
                    interface_id=previous,
                    fencing_generation=generation,
                )
                self.record(
                    "TASK_STATE",
                    task_id=task.task_id,
                    state="QUEUED",
                    trigger="lease_expired",
                    operator="hub",
                    previous_interface=previous,
                    revoked_fencing_generation=generation,
                )
                self.schedule_dispatch()
                return
        except asyncio.CancelledError:
            return

    async def _task_timeout(self, task: Task, generation: int) -> None:
        try:
            await asyncio.sleep(float(task.envelope["timeout_s"]))
            if (
                task.fencing_generation == generation
                and task.state in {"CLAIMED", "RUNNING"}
            ):
                self.record(
                    "TASK_TIMEOUT",
                    task_id=task.task_id,
                    interface_id=task.selected_interface,
                    fencing_generation=generation,
                    timeout_s=task.envelope["timeout_s"],
                )
                await self._terminal_fail(
                    task,
                    "TIMED_OUT",
                    "retryable",
                    "task_timeout",
                    operator="hub",
                )
        except asyncio.CancelledError:
            return

    async def on_event(self, msg: Msg) -> None:
        if self.events_paused_until > time.time():
            await asyncio.sleep(self.events_paused_until - time.time())
        request = self._decode(msg)
        interface_id = str((request or {}).get("interface_id", ""))
        session = self.sessions.get(interface_id)
        event = (request or {}).get("event")
        if (
            not request
            or not isinstance(event, dict)
            or not session
            or not session.token
            or not verify_proof(session.token, request)
        ):
            self.json_response(msg, {"ok": False, "reason": "unauthorized"})
            return
        task_id = str(event.get("task_id", ""))
        task = self.tasks.get(task_id)
        seq = int(event.get("seq", -1))
        generation = int(event.get("fencing_generation", -1))
        kind = event.get("kind")
        if not task:
            self.json_response(msg, {"ok": False, "reason": "unknown_task"})
            return
        if kind not in VALID_EVENT_KINDS:
            self._reject_event(task_id, interface_id, seq, generation, "invalid_kind")
            self.json_response(msg, {"ok": False, "reason": "invalid_kind"})
            return
        if generation != task.fencing_generation:
            self._reject_event(task_id, interface_id, seq, generation, "fencing_stale")
            self.json_response(
                msg,
                {
                    "ok": False,
                    "reason": "fencing_stale",
                    "current_generation": task.fencing_generation,
                },
            )
            return
        if seq <= task.event_seq:
            self.record(
                "EVENT_DUPLICATE",
                task_id=task_id,
                interface_id=interface_id,
                seq=seq,
                kind=kind,
            )
            self.json_response(
                msg,
                {"ok": True, "duplicate": True, "last_seq": task.event_seq},
            )
            return
        if seq != task.event_seq + 1:
            self._reject_event(
                task_id,
                interface_id,
                seq,
                generation,
                "seq_gap",
                expected_seq=task.event_seq + 1,
            )
            self.json_response(
                msg,
                {
                    "ok": False,
                    "reason": "seq_gap",
                    "expected_seq": task.event_seq + 1,
                },
            )
            return
        if task.selected_interface != interface_id:
            self._reject_event(
                task_id, interface_id, seq, generation, "not_current_interface"
            )
            self.json_response(
                msg, {"ok": False, "reason": "not_current_interface"}
            )
            return
        if task.state in TERMINAL_STATES and kind != "completed":
            self._reject_event(
                task_id, interface_id, seq, generation, "task_terminal"
            )
            self.json_response(msg, {"ok": False, "reason": "task_terminal"})
            return
        if kind == "result_ready":
            pointer = (event.get("payload") or {}).get("pointer")
            pointer_ok, pointer_reason = self._validate_pointer(pointer)
            if not pointer_ok:
                self._reject_event(
                    task_id,
                    interface_id,
                    seq,
                    generation,
                    pointer_reason,
                )
                self.json_response(
                    msg, {"ok": False, "reason": pointer_reason}
                )
                return

        self.record(
            "EVENT_ACCEPTED",
            task_id=task_id,
            interface_id=interface_id,
            seq=seq,
            kind=kind,
            fencing_generation=generation,
            payload=event.get("payload"),
        )
        self.record(
            "TASK_EVENT",
            task_id=task_id,
            interface_id=interface_id,
            seq=seq,
            kind=kind,
            fencing_generation=generation,
        )
        task.event_seq = seq

        if kind == "claimed":
            task.state = "RUNNING"
            task.lease_expires_at = time.time() + self._lease_for(task)
            self.record(
                "TASK_STATE",
                task_id=task_id,
                state="RUNNING",
                trigger="claimed_event",
                operator=interface_id,
                interface_id=interface_id,
                fencing_generation=generation,
                event_seq=seq,
            )
        elif kind == "heartbeat":
            task.lease_expires_at = time.time() + self._lease_for(task)
        elif kind == "progress":
            task.lease_expires_at = time.time() + self._lease_for(task)
        elif kind == "result_ready":
            pointer = event["payload"]["pointer"]
            task.result_pointer = pointer
            task.state = "SUCCEEDED"
            task.updated_at = now_iso()
            task.terminal_reason = "execution_declared"
            if task.lease_timer:
                task.lease_timer.cancel()
            if task.timeout_timer:
                task.timeout_timer.cancel()
            self.record(
                "TASK_STATE",
                task_id=task_id,
                state="SUCCEEDED",
                trigger="result_ready",
                operator=interface_id,
                interface_id=interface_id,
                fencing_generation=generation,
                event_seq=seq,
                result_pointer=pointer,
            )
            self.schedule_dispatch()
        elif kind == "completed":
            if task.state != "SUCCEEDED":
                await self._terminal_fail(
                    task,
                    "FAILED",
                    "non_retryable",
                    "completed_without_result",
                    operator=interface_id,
                )
        elif kind == "failed":
            payload = event.get("payload") or {}
            category = payload.get("reason_category", "non_retryable")
            reason_code = payload.get("reason_code", "unspecified")
            await self._terminal_fail(
                task,
                "FAILED",
                category,
                reason_code,
                operator=interface_id,
            )
        elif kind == "cancelled":
            await self._terminal_fail(
                task,
                "CANCELLED",
                "non_retryable",
                "operator_cancelled",
                operator=interface_id,
            )
        self.json_response(
            msg,
            {"ok": True, "task_id": task_id, "seq": seq, "kind": kind},
        )

    def _lease_for(self, task: Task) -> float:
        params = task.envelope.get("params") or {}
        if isinstance(params, dict) and "_lease_seconds" in params:
            return float(params["_lease_seconds"])
        return self.default_lease_seconds

    def _reject_event(
        self,
        task_id: str,
        interface_id: str,
        seq: int,
        generation: int,
        reason: str,
        **extra: Any,
    ) -> None:
        self.record(
            "EVENT_REJECTED",
            task_id=task_id,
            interface_id=interface_id,
            seq=seq,
            fencing_generation=generation,
            reason=reason,
            **extra,
        )

    def _run_three_layer(
        self, task: "Task", request: dict[str, Any]
    ) -> dict[str, Any]:
        """三层验收（I-4）：从任务信封的 output_contract 与实际产物执行验证。

        返回 {"enforced": bool, "result": dict|None, "reject": str|None}
        - enforced: 信封里是否带 output_contract（带则失败即拒绝裁定）
        - reject:   非空表示应拒绝本次裁定，值为拒绝原因
        """
        params = (task.envelope or {}).get("params") or {}
        contract = params.get("output_contract") or {}
        enforce_flag = bool(request.get("enforce_three_layer"))
        enforced = bool(contract) or enforce_flag
        if not contract and not enforce_flag:
            return {"enforced": False, "result": None, "reject": None}

        import sys as _sys

        stage2 = Path(__file__).resolve().parent.parent / "stage2"
        if str(stage2) not in _sys.path:
            _sys.path.insert(0, str(stage2))
        try:
            from three_layer_verdict import ThreeLayerVerdict
        except Exception as exc:  # 模块不可用时不阻断旧路径
            return {"enforced": False, "result": None,
                    "reject": None, "load_error": "three_layer_unavailable:%s" % exc}

        # 读取实际产物
        output: dict[str, Any] = {}
        pointer = task.result_pointer or {}
        load_error = None
        if pointer:
            try:
                path = file_uri_to_path(pointer.get("uri", ""))
                raw = path.read_bytes()
                if "json" in str(pointer.get("media_type", "")):
                    try:
                        parsed = json.loads(raw.decode("utf-8", errors="replace"))
                        output = parsed if isinstance(parsed, dict) else {"value": parsed}
                    except Exception:
                        output = {"text": raw.decode("utf-8", errors="replace")}
                else:
                    output = {"text": raw.decode("utf-8", errors="replace")}
            except Exception as exc:
                load_error = "result_unreadable:%s" % exc

        verdict_word = request.get("verdict", "")
        checker = request.get("checker") or "unknown"
        implementer = task.selected_interface or "unknown"

        v = ThreeLayerVerdict({"output_contract": contract})
        try:
            result = v.execute_all(
                output,
                {},
                request.get("run_outputs") or [],
                verdict_word,
                checker,
                implementer,
            )
        except Exception as exc:
            return {"enforced": enforced, "result": None,
                    "reject": "three_layer_error:%s" % exc}

        if load_error:
            result["load_error"] = load_error

        reject = None
        if enforced:
            if not result["layer1"]["pass"]:
                reject = "layer1_deterministic_failed"
            elif result["layer2"].get("pass") is False:
                reject = "layer2_consistency_below_threshold"
            elif not result["layer3"]["pass"]:
                reject = ("verifier_is_implementer"
                          if not result["layer3"]["independent"]
                          else "verdict_not_canonical")
        return {"enforced": enforced, "result": result, "reject": reject}

    def _validate_pointer(
        self, pointer: Any
    ) -> tuple[bool, str]:
        if not isinstance(pointer, dict):
            return False, "invalid_result_pointer"
        uri = pointer.get("uri")
        expected = pointer.get("sha256")
        size = pointer.get("bytes")
        mime = pointer.get("media_type")
        if (
            not isinstance(uri, str)
            or not isinstance(expected, str)
            or not isinstance(size, int)
            or not isinstance(mime, str)
        ):
            return False, "invalid_result_pointer"
        try:
            path = file_uri_to_path(uri)
        except ValueError:
            return False, "unsupported_result_uri"
        if not path.is_file():
            return False, "result_missing"
        if path.stat().st_size != size:
            return False, "result_size_mismatch"
        if sha256_file(path) != expected:
            return False, "result_hash_mismatch"
        return True, "ok"

    async def _terminal_fail(
        self,
        task: Task,
        state: str,
        reason_category: str,
        reason_code: str,
        operator: str,
    ) -> None:
        if task.state in TERMINAL_STATES:
            return
        task.state = state
        task.updated_at = now_iso()
        task.terminal_reason = reason_code
        task.lease_expires_at = None
        if task.lease_timer:
            task.lease_timer.cancel()
        if task.timeout_timer:
            task.timeout_timer.cancel()
        self.record(
            "TASK_STATE",
            task_id=task.task_id,
            state=state,
            trigger="terminal",
            operator=operator,
            interface_id=task.selected_interface,
            fencing_generation=task.fencing_generation,
            reason=reason_code,
            reason_category=reason_category,
        )
        if reason_category == "non_retryable":
            self.record(
                "DEAD_LETTER",
                task_id=task.task_id,
                reason_code=reason_code,
                envelope=task.envelope,
            )
        # 失败/超时/取消也要进团队记忆，否则「卡死」永远无法被检索
        self._spawn_terminal_memory(task, state, reason_code)
        self.schedule_dispatch()

    async def on_state(self, msg: Msg) -> None:
        request = self._decode(msg)
        task_id = str((request or {}).get("task_id", ""))
        task = self.tasks.get(task_id)
        if not task:
            self.json_response(msg, {"ok": False, "reason": "not_found"})
            return
        self.json_response(
            msg,
            {
                "ok": True,
                "task_id": task_id,
                "state": task.state,
                "state_history": [
                    row
                    for row in self.ledger.records()
                    if row.get("record") == "TASK_STATE"
                    and row.get("task_id") == task_id
                ],
                "events": [
                    row
                    for row in self.ledger.records()
                    if row.get("record") == "TASK_EVENT"
                    and row.get("task_id") == task_id
                ],
                "event_seq": task.event_seq,
                "fencing_generation": task.fencing_generation,
                "result_pointer": task.result_pointer,
                "verdict": task.verdict,
                "trace_id": task.trace_id,
            },
        )

    async def on_verdict(self, msg: Msg) -> None:
        request = self._decode(msg)
        if not request:
            self.json_response(msg, {"ok": False, "reason": "invalid_verdict"})
            return
        task_id = str(request.get("task_id", ""))
        verdict = request.get("verdict")
        task = self.tasks.get(task_id)
        if not task:
            self.json_response(msg, {"ok": False, "reason": "not_found"})
            return
        if verdict not in VERDICT_WORDS:
            self.json_response(
                msg,
                {
                    "ok": False,
                    "reason": "invalid_verdict_word",
                    "allowed": sorted(VERDICT_WORDS),
                },
            )
            return
        # 未到终态的任务不得判「通过」：没有产出就宣布成功是伪证。
        # 允许「不通过 / 作废 / 未核」——那是记录问题，不是宣布完成。
        if task.state not in TERMINAL_STATES and verdict in ("通过", "带注通过"):
            self.record(
                "VERDICT_REJECTED",
                task_id=task_id,
                reason="task_not_terminal",
                checker=request.get("checker", "unknown"),
                execution_state=task.state,
            )
            self.json_response(
                msg,
                {
                    "ok": False,
                    "reason": "task_not_terminal",
                    "execution_state": task.state,
                    "allowed_verdicts": ["不通过", "未核", "作废"],
                },
            )
            return

        if verdict == "未核":
            missing = [
                key
                for key in ("owner", "min_action", "due_at")
                if not request.get(key)
            ]
            if missing:
                self.json_response(
                    msg,
                    {
                        "ok": False,
                        "reason": "unverified_exit_incomplete",
                        "missing": missing,
                    },
                )
                return
        # I-4: 三层验收（信封带 output_contract 时强制，否则仅记录建议值）
        if task.state in TERMINAL_STATES:
            three_layer = self._run_three_layer(task, request)
            if three_layer.get("reject"):
                self.record(
                    "VERDICT_REJECTED",
                    task_id=task_id,
                    reason=three_layer["reject"],
                    checker=request.get("checker", "unknown"),
                    three_layer=three_layer.get("result"),
                )
                self.json_response(
                    msg,
                    {
                        "ok": False,
                        "reason": three_layer["reject"],
                        "three_layer": three_layer.get("result"),
                    },
                )
                return
        else:
            three_layer = {"enforced": False, "result": None, "reject": None}

        record = self.record(
            "VERDICT",
            task_id=task_id,
            verdict=verdict,
            three_layer=three_layer.get("result"),
            three_layer_enforced=three_layer.get("enforced", False),
            checker=request.get("checker", "unknown"),
            evidence_level=request.get("evidence_level", "E3"),
            deterministic_check=request.get("deterministic_check"),
            independent_review=request.get("independent_review"),
            physical_readback=request.get("physical_readback"),
            notes=request.get("notes"),
            owner=request.get("owner"),
            min_action=request.get("min_action"),
            due_at=request.get("due_at"),
            execution_state=task.state,
        )
        task.verdict = record
        graph_memory = await self._record_graph_outcome(task, verdict)
        self.json_response(
            msg,
            {
                "ok": True,
                "verdict_record": record,
                "graph_memory": graph_memory,
            },
        )

    async def _record_graph_outcome(self, task: "Task", verdict: str) -> dict[str, Any]:
        """UAI 记忆管道：裁定 → outbox → 图谱。

        与旧实现的区别：**先持久化入队，再尝试投递**。图谱慢/挂不阻塞裁定，
        没写成的记忆留在 outbox 里等重试，而不是只剩一条 MEMORY_OUTCOME 就永久丢失。
        """
        if not _graph_writes_enabled():
            return {"written": False, "reason": "disabled_by_env"}
        try:
            pipeline = _load_memory_pipeline()
        except Exception as exc:
            return {"written": False, "reason": "pipeline_unavailable:%s" % str(exc)[:100]}

        envelope = task.envelope or {}
        # 只入队不投递：裁定不能被图谱 I/O 拖住（Graphiti 冷启动实测 4.8s）。
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    pipeline.record_verdict,
                    task_id=task.task_id,
                    verdict=verdict,
                    agent_id=task.selected_interface or "unknown",
                    description=envelope.get("instruction") or "",
                    flush=False,
                ),
                timeout=self.graph_memory_timeout_s,
            )
        except asyncio.TimeoutError:
            result = {"written": False, "reason": "enqueue_timeout"}
        except Exception as exc:
            result = {"written": False, "reason": "error:%s" % str(exc)[:100]}
        else:
            self._spawn_memory_drain()

        deferred = (
            result.get("status") == "pending"
            or result.get("reason") == "deferred"
            or bool(result.get("enqueued"))
            # 同一 task+verdict 重复提交是正常的客户端重试，不是记忆写丢
            or bool(result.get("duplicate"))
            or result.get("reason") == "duplicate_intent_id"
        )
        if not result.get("written") and not deferred:
            # 只有「真的没入队」才算记忆写丢；入队成功但还没投递不是失败，
            # 否则每条正常裁定都会写一条 MEMORY_OUTCOME，把真故障淹掉。
            self.record(
                "MEMORY_OUTCOME",
                task_id=task.task_id,
                verdict=verdict,
                reason=result.get("reason"),
                key=result.get("key"),
                intent_id=result.get("intent_id"),
            )
        return result

    # ---------- 团队记忆：终态事件自动入图 ----------

    def _spawn_memory_drain(self) -> None:
        """马上补一次投递，不等 60s 周期。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._drain_memory_once())
        self._memory_tasks.add(task)
        task.add_done_callback(self._memory_tasks.discard)

    def _spawn_terminal_memory(self, task: "Task", state: str, reason_code: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        spawned = loop.create_task(
            self._record_terminal_memory(task, state, reason_code)
        )
        self._memory_tasks.add(spawned)

        def _done(finished: asyncio.Task[Any]) -> None:
            self._memory_tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None:
                # 后台任务异常不能静默：账本留痕，否则「记忆写丢」永远查不到
                self.record(
                    "MEMORY_OUTCOME",
                    task_id=task.task_id,
                    state=state,
                    reason="background_error:%s" % str(exc)[:120],
                )

        spawned.add_done_callback(_done)

    async def _record_terminal_memory(
        self, task: "Task", state: str, reason_code: str
    ) -> None:
        """失败/超时/取消也要留下可检索的记忆；永远不阻塞中枢。"""
        if not _graph_writes_enabled():
            return
        try:
            pipeline = _load_memory_pipeline()
        except Exception:
            return
        envelope = task.envelope or {}
        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(
                    pipeline.record_terminal,
                    task_id=task.task_id,
                    state=state,
                    agent_id=task.selected_interface or "unknown",
                    description=envelope.get("instruction") or "",
                    reason=reason_code,
                ),
                timeout=self.graph_memory_timeout_s,
            )
        except asyncio.TimeoutError:
            self.record(
                "MEMORY_OUTCOME",
                task_id=task.task_id,
                state=state,
                reason="timeout_queued",
            )
            return
        except Exception as exc:
            self.record(
                "MEMORY_OUTCOME",
                task_id=task.task_id,
                state=state,
                reason="error:%s" % str(exc)[:120],
            )
            return
        if not result.get("written"):
            self.record(
                "MEMORY_OUTCOME",
                task_id=task.task_id,
                state=state,
                reason=result.get("reason"),
                intent_id=result.get("intent_id"),
            )

    async def _drain_memory_once(self) -> dict[str, Any]:
        if not _graph_writes_enabled():
            return {"skipped": "disabled_by_env"}
        try:
            from memory_outbox import MemoryOutbox
            from memory_writer import drain
        except Exception as exc:
            return {"error": "import_failed:%s" % str(exc)[:80]}
        try:
            outbox = MemoryOutbox(None)
            return await asyncio.to_thread(drain, outbox, limit=20)
        except Exception as exc:
            return {"error": str(exc)[:120]}

    async def _memory_drain_loop(self) -> None:
        """outbox 里没写成的记忆靠它自愈，不需要人记得手动 flush。"""
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(self.memory_drain_s)
                if self.stop_event.is_set():
                    return
                await self._drain_memory_once()
            except asyncio.CancelledError:
                return
            except Exception:
                continue

    async def on_debug(self, msg: Msg) -> None:
        request = self._decode(msg)
        if not request:
            self.json_response(msg, {"ok": False, "reason": "invalid_debug_request"})
            return
        action = request.get("action")
        if action == "pause_events":
            seconds = max(0.0, min(10.0, float(request.get("seconds", 1))))
            self.events_paused_until = time.time() + seconds
            self.json_response(msg, {"ok": True, "seconds": seconds})
            return
        if action == "redispatch":
            task = self.tasks.get(str(request.get("task_id", "")))
            session = (
                self.sessions.get(task.selected_interface or "")
                if task
                else None
            )
            if not task or not session:
                self.json_response(msg, {"ok": False, "reason": "not_available"})
                return
            ok, _ = await self._send_dispatch(task, session, debug=True)
            self.json_response(msg, {"ok": ok})
            return
        if action == "snapshot":
            self.json_response(
                msg,
                {
                    "ok": True,
                    "tasks": {
                        task_id: {
                            "state": task.state,
                            "event_seq": task.event_seq,
                            "fencing_generation": task.fencing_generation,
                            "selected_interface": task.selected_interface,
                        }
                        for task_id, task in self.tasks.items()
                    },
                    "interfaces": {
                        interface_id: {
                            "active": session.active,
                            "stale": session.stale,
                            "last_heartbeat": session.last_heartbeat,
                        }
                        for interface_id, session in self.sessions.items()
                    },
                },
            )
            return
        self.json_response(msg, {"ok": False, "reason": "unknown_debug_action"})

    async def on_reconcile(self, msg: Msg) -> None:
        # 性能与正确性修复（2026-11-18）：原实现是「每个 task 扫一遍全账本」，
        # 复杂度 O(tasks x ledger)。账本 5500+ 行时单次调用会把 hub 的事件循环
        # **整段堵死**，于是 reconcile 超时的同时 submit/claim 也全部超时 ——
        # 进程、锁、心跳全绿，中枢实际已瘫痪（今天「假在线」的真正主因）。
        # 现在改成一次遍历建索引，再逐 task 比对。
        seq_by_task: dict[str, set[int]] = {}
        for row in self.ledger.records():
            if row.get("record") != "TASK_EVENT":
                continue
            task_id = row.get("task_id")
            if task_id is None:
                continue
            try:
                seq = int(row["seq"])
            except (KeyError, TypeError, ValueError):
                continue
            seq_by_task.setdefault(str(task_id), set()).add(seq)

        gaps: list[dict[str, Any]] = []
        for task in self.tasks.values():
            seqs = seq_by_task.get(task.task_id)
            if not seqs:
                continue
            highest = max(seqs)
            # 不物化完整缺失列表：seq 异常大时 range(1, highest+1) 会长时间占用事件循环。
            # 只在必要范围内扫描并提前截断（codex 审代码第 3 条）。
            scan_ceiling = min(highest, 10000)
            missing: list[int] = []
            missing_count = 0
            for seq in range(1, scan_ceiling + 1):
                if seq in seqs:
                    continue
                missing_count += 1
                if len(missing) < 50:
                    missing.append(seq)
            truncated = highest > scan_ceiling
            if missing_count or truncated:
                # 不把未扫描的序号算成缺失（那会高估），单独用 truncated/unscanned 标注
                gaps.append({"task_id": task.task_id, "missing": missing,
                             "missing_count": missing_count,
                             "scanned_to": scan_ceiling,
                             "truncated": truncated,
                             "unscanned": max(0, highest - scan_ceiling)})
        self.record(
            "RECONCILIATION",
            gap_count=len(gaps),
            gaps=gaps[:50],
            frequency_s=300,
        )
        self.json_response(msg, {"ok": True, "gap_count": len(gaps), "gaps": gaps[:50]})

        # 保证上面这段不会在异常时静默吞掉
    async def run(self) -> None:
        await self.connect()

        # A-09：持锁期间周期性续期。TTL 只用于告警（stale/wedged），不再用于夺权，
        # 所以这里只需要让 status() 别误报；真裁决在 OS 锁上。
        guard = getattr(self, "_guard", None)
        heartbeat_task: asyncio.Task[Any] | None = None
        if guard is not None:
            async def _singleton_beat() -> None:
                while True:
                    await asyncio.sleep(max(5.0, float(guard.ttl_s) / 3.0))
                    try:
                        guard.heartbeat()
                    except Exception:
                        return

            heartbeat_task = asyncio.create_task(_singleton_beat())
        async def _dispatch_watchdog() -> None:
            """派发看护：有 QUEUED 却没在派发，就补一次 schedule_dispatch。

            这是对 dispatch_scheduled 卡死的第二道保险：即便标志位被意外置 True，
            看护也能发现「有活却没派」并把它救回来。"""
            while True:
                await asyncio.sleep(5.0)
                try:
                    pending = any(t.state == "QUEUED" for t in self.tasks.values())
                    if pending and not self.dispatch_scheduled:
                        self.schedule_dispatch()
                except Exception:
                    pass

        dispatch_task = asyncio.create_task(_dispatch_watchdog())

        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(
                    signal_name, lambda: self.stop_event.set()
                )
        print(
            json.dumps(
                {
                    "event": "hub_ready",
                    "pid": os.getpid(),
                    "nats_url": self.nats_url,
                    "contract_version": CONTRACT_VERSION,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        await self.stop_event.wait()
        dispatch_task.cancel()
        if heartbeat_task is not None:
            heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await heartbeat_task
        if self.nc:
            await self.nc.drain()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument(
        "--nats-url",
        default=os.environ.get("INTERFACE_PROTO_NATS_URL", "nats://127.0.0.1:43321"),
    )
    parser.add_argument("--lease-seconds", type=float, default=300.0)
    return parser.parse_args()


EXIT_ALREADY_RUNNING = 3


def _acquire_hub_singleton(base: Path):
    """A-09：同一个 base 只允许一个中枢持有写权。"""
    import sys as _sys

    stage2 = Path(__file__).resolve().parent.parent / "stage2"
    if str(stage2) not in _sys.path:
        _sys.path.insert(0, str(stage2))
    try:
        from hub_singleton import HubSingleton
    except Exception as exc:
        return None, {"reason": "singleton_unavailable:%s" % str(exc)[:100]}

    guard = HubSingleton(
        base / "data" / "hub.lock",
        namespace="uai-hub",
        ttl_s=float(os.environ.get("UAI_HUB_SINGLETON_TTL", "600")),
    )
    ok, info = guard.acquire()
    return (guard, info) if ok else (None, info)


def main() -> int:
    args = parse_args()
    base = args.base.resolve()
    guard, info = _acquire_hub_singleton(base)
    if guard is None:
        # 第二个中枢**不许**降级成只读继续订阅：那会双派、双写账本。
        # 2026-11-17 实测过：两个中枢各写一份 TASK_ACCEPTED / VERDICT。
        print(
            json.dumps(
                {
                    "event": "hub_singleton_denied",
                    "reason": info.get("reason"),
                    "holder": info.get("holder"),
                    "pid": os.getpid(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return EXIT_ALREADY_RUNNING
    hub = Hub(base, args.nats_url, args.lease_seconds)
    hub._guard = guard  # run() 里续期，退出时释放
    try:
        asyncio.run(hub.run())
    except KeyboardInterrupt:
        return 130
    finally:
        guard.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
