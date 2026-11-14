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
        self.ledger_path = base / "data" / "ledger.jsonl"
        self.ledger = AppendOnlyJsonl(self.ledger_path)
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
        if (
            not request
            or not session
            or not session.token
            or not verify_proof(session.token, request)
        ):
            self.json_response(msg, {"ok": False, "reason": "unauthorized"})
            return
        session.last_heartbeat = time.monotonic()
        self.record("INTERFACE_HEARTBEAT", interface_id=interface_id)
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
            asyncio.create_task(self._dispatch_round())

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
        record = self.record(
            "VERDICT",
            task_id=task_id,
            verdict=verdict,
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
        self.json_response(msg, {"ok": True, "verdict_record": record})

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
        gaps: list[dict[str, Any]] = []
        for task in self.tasks.values():
            seqs = sorted(
                {
                    int(row["seq"])
                    for row in self.ledger.records()
                    if row.get("record") == "TASK_EVENT"
                    and row.get("task_id") == task.task_id
                }
            )
            if not seqs:
                continue
            expected = list(range(1, max(seqs) + 1))
            missing = sorted(set(expected) - set(seqs))
            if missing:
                gaps.append({"task_id": task.task_id, "missing": missing})
        self.record(
            "RECONCILIATION",
            gap_count=len(gaps),
            gaps=gaps,
            frequency_s=300,
        )
        self.json_response(msg, {"ok": True, "gap_count": len(gaps), "gaps": gaps})

    async def run(self) -> None:
        await self.connect()
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


def main() -> int:
    args = parse_args()
    hub = Hub(args.base.resolve(), args.nats_url, args.lease_seconds)
    try:
        asyncio.run(hub.run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
