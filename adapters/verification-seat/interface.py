#!/usr/bin/env python3
"""Executor interface process for the v0.2 contract prototype."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import mimetypes
import os
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import nats
from nats.aio.client import Client as NATS
from nats.aio.msg import Msg

from core import (
    CONTRACT_VERSION,
    AppendOnlyJsonl,
    append_log,
    approval_binding_sha256,
    canonical_json,
    file_uri_to_path,
    load_or_create_secret,
    make_proof,
    now_iso,
    path_to_file_uri,
    sha256_bytes,
    sha256_file,
    sign_value,
    validate_approval_grant,
    validate_envelope,
    verify_value,
)


def safe_id(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in value)


@dataclass
class DispatchRecord:
    task_id: str
    envelope: dict[str, Any] | None
    state: str
    execution_count: int


class InterfaceState:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_path = data_dir / "inbox.jsonl"
        self.events_path = data_dir / "outbox.jsonl"
        self.acks_path = data_dir / "outbox-acks.jsonl"
        self.operations_path = data_dir / "operations.jsonl"
        self.dispatch_path = data_dir / "dispatch.jsonl"
        self.connection_path = data_dir / "connection.jsonl"
        self.events = AppendOnlyJsonl(self.events_path)
        self.acks = AppendOnlyJsonl(self.acks_path)
        self.inbox = AppendOnlyJsonl(self.inbox_path)
        self.operations = AppendOnlyJsonl(self.operations_path)
        self.dispatches = AppendOnlyJsonl(self.dispatch_path)
        self._lock = asyncio.Lock()
        self.tasks: dict[str, DispatchRecord] = {}
        self._load_inbox()

    def _load_inbox(self) -> None:
        for row in self.inbox.records():
            task_id = row.get("task_id")
            if not task_id:
                continue
            current = self.tasks.get(task_id, DispatchRecord(task_id, None, "new", 0))
            record = row.get("record")
            if record == "DISPATCH_ACCEPTED":
                current.envelope = row.get("envelope")
                current.state = "accepted"
            elif record == "EXECUTION_STARTED":
                current.state = "running"
                current.execution_count += 1
            elif record in {"EXECUTION_COMPLETED", "EXECUTION_FAILED"}:
                current.state = "completed" if record == "EXECUTION_COMPLETED" else "failed"
            elif record == "EXECUTION_NOOP_DUPLICATE":
                current.state = "completed"
            self.tasks[task_id] = current

    async def accept_dispatch(
        self, envelope: dict[str, Any], dispatch_id: str
    ) -> tuple[bool, DispatchRecord]:
        async with self._lock:
            task_id = str(envelope["task_id"])
            current = self.tasks.get(task_id)
            if current:
                self.dispatches.append(
                    {
                        "record": "DISPATCH_DUPLICATE",
                        "ts": now_iso(),
                        "task_id": task_id,
                        "dispatch_id": dispatch_id,
                        "execution_count": current.execution_count,
                    }
                )
                return False, current
            current = DispatchRecord(
                task_id=task_id,
                envelope=envelope,
                state="accepted",
                execution_count=0,
            )
            self.tasks[task_id] = current
            self.inbox.append(
                {
                    "record": "DISPATCH_ACCEPTED",
                    "ts": now_iso(),
                    "task_id": task_id,
                    "dispatch_id": dispatch_id,
                    "envelope": envelope,
                }
            )
            self.dispatches.append(
                {
                    "record": "DISPATCH_ACCEPTED",
                    "ts": now_iso(),
                    "task_id": task_id,
                    "dispatch_id": dispatch_id,
                }
            )
            return True, current

    async def execution_started(self, task_id: str) -> None:
        async with self._lock:
            current = self.tasks[task_id]
            current.state = "running"
            current.execution_count += 1
            self.inbox.append(
                {
                    "record": "EXECUTION_STARTED",
                    "ts": now_iso(),
                    "task_id": task_id,
                    "execution_count": current.execution_count,
                }
            )

    async def execution_finished(self, task_id: str, ok: bool, detail: str) -> None:
        async with self._lock:
            current = self.tasks[task_id]
            current.state = "completed" if ok else "failed"
            self.inbox.append(
                {
                    "record": "EXECUTION_COMPLETED" if ok else "EXECUTION_FAILED",
                    "ts": now_iso(),
                    "task_id": task_id,
                    "detail": detail,
                }
            )

    def pending_events(self) -> list[dict[str, Any]]:
        acked = {
            row["event_id"]
            for row in self.acks.records()
            if row.get("record") == "ACK" and row.get("ok") is True
        }
        pending = [
            row
            for row in self.events.records()
            if row.get("record") == "EVENT" and row.get("event_id") not in acked
        ]
        return sorted(
            pending,
            key=lambda row: (row.get("created_at", ""), row["task_id"], row["seq"]),
        )

    def append_event(
        self,
        *,
        interface_id: str,
        task_id: str,
        trace_id: str,
        seq: int,
        fencing_generation: int,
        kind: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event = {
            "task_id": task_id,
            "trace_id": trace_id,
            "seq": seq,
            "fencing_generation": fencing_generation,
            "kind": kind,
            "payload": payload or {},
            "created_at": now_iso(),
        }
        row = {
            "record": "EVENT",
            "event_id": f"{task_id}:{seq}",
            "created_at": event["created_at"],
            **event,
        }
        self.events.append(row)
        return event

    def ack_event(
        self, event_id: str, ok: bool, response: dict[str, Any], timed_out: bool = False
    ) -> None:
        self.acks.append(
            {
                "record": "RETRY" if timed_out else "ACK",
                "ts": now_iso(),
                "event_id": event_id,
                "ok": ok,
                "timed_out": timed_out,
                "response": response,
            }
        )


class InterfaceProcess:
    def __init__(
        self,
        *,
        base: Path,
        interface_id: str,
        token_file: Path,
        nats_url: str,
        heartbeat_s: float,
        task_heartbeat_s: float,
        event_timeout_s: float,
        max_concurrency: int,
        disable_heartbeat: bool,
        suppress_echo: bool,
        executor: str,
        runtime_executable: str,
    ):
        self.base = base
        self.interface_id = interface_id
        self.token_file = token_file
        self.token = token_file.read_text(encoding="utf-8").strip()
        self.nats_url = nats_url
        self.heartbeat_s = heartbeat_s
        self.task_heartbeat_s = task_heartbeat_s
        self.event_timeout_s = event_timeout_s
        self.max_concurrency = max_concurrency
        self.disable_heartbeat = disable_heartbeat
        self.suppress_echo = suppress_echo
        self.executor = executor
        self.runtime_executable = runtime_executable
        self.data_dir = base / "runtime" / "interfaces" / safe_id(interface_id)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.data_dir / "work"
        self.results_dir = self.data_dir / "results"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.control_subject = f"iface.{safe_id(interface_id)}.control"
        self.approval_secret_path = base / "data" / "approval-signing.key"
        self.approval_secret = load_or_create_secret(self.approval_secret_path)
        self.state = InterfaceState(self.data_dir)
        self.core_op_log = AppendOnlyJsonl(self.data_dir / "operations.jsonl")
        self.nc: NATS | None = None
        self.registered = False
        self.flush_complete = asyncio.Event()
        self.flush_complete.set()
        self.stop_event = asyncio.Event()
        self.inflight: set[str] = set()
        self.active_tasks: dict[str, asyncio.Task[Any]] = {}
        self.registration_lock = asyncio.Lock()
        self.outbox_lock = asyncio.Lock()
        self._stopping = False

    def log_connection(self, message: str, **fields: Any) -> None:
        append_log(
            self.state.connection_path,
            f"interface:{self.interface_id}",
            message,
            **fields,
        )

    def operation(
        self, task_id: str, action: str, object_value: str, result: str
    ) -> None:
        self.core_op_log.append(
            {
                "ts": now_iso(),
                "interface_id": self.interface_id,
                "task_id": task_id,
                "action": action,
                "object": object_value,
                "result": result,
            }
        )

    def capability_card(self) -> dict[str, Any]:
        if self.executor == "verification-seat":
            capability = {
                "name": "readonly_verification",
                "description": "Read-only evidence verification for the v0.2 lab.",
                "input_types": ["application/json", "text/plain"],
                "output_types": ["application/json"],
                "max_concurrency": self.max_concurrency,
                "duration_class": "short",
            }
            agent = {
                "vendor": "verification-seat-lab",
                "runtime": "verification-seat-contract-adapter",
                "version": "0.2-lab",
            }
            sandbox = {
                "isolated": True,
                "type": "task-scoped-readonly",
                "network": "disabled-by-task-policy",
                "code_execution_allowed": False,
                "read_only_inputs": True,
            }
        else:
            capability = {
                "name": "runtime_exec",
                "description": "Execute a runtime CLI task and return a result pointer.",
                "input_types": ["text/plain", "application/json"],
                "output_types": ["application/json", "text/plain"],
                "max_concurrency": self.max_concurrency,
                "duration_class": "medium",
            }
            agent = {
                "vendor": "external-runtime",
                "runtime": "runtime-cli",
                "version": "runtime-provided",
            }
            sandbox = {
                "isolated": True,
                "type": "runtime-read-only-sandbox",
                "network": "disabled-by-task-policy",
                "code_execution_allowed": True,
            }
        return {
            "interface_id": self.interface_id,
            "contract_version": CONTRACT_VERSION,
            "agent": agent,
            "capabilities": [capability],
            "sandbox": sandbox,
            "transport": {
                "type": "nats-core-request-reply",
                "control_subject": self.control_subject,
                "direction": "outbound-from-interface",
            },
            "auth": {"method": "hmac-sha256", "auth_id": self.interface_id},
            "heartbeat_s": self.heartbeat_s,
        }

    async def connect(self) -> None:
        async def disconnected_cb() -> None:
            self.registered = False
            self.flush_complete.clear()
            self.log_connection("connection_down", nats_url=self.nats_url)

        async def reconnected_cb() -> None:
            self.log_connection(
                "connection_up",
                nats_url=self.nats_url,
                server=getattr(self.nc, "connected_url", None)
                if self.nc
                else None,
            )
            self.flush_complete.clear()
            await self.register()

        async def error_cb(error: Exception) -> None:
            self.log_connection(
                "connection_error",
                error_type=type(error).__name__,
                error=str(error)[:300],
            )

        async def closed_cb() -> None:
            self.registered = False
            self.log_connection("connection_closed")

        self.nc = await nats.connect(
            self.nats_url,
            name=f"verification-seat-interface-v02-{safe_id(self.interface_id)}",
            connect_timeout=2,
            reconnect_time_wait=1,
            max_reconnect_attempts=-1,
            allow_reconnect=True,
            disconnected_cb=disconnected_cb,
            reconnected_cb=reconnected_cb,
            error_cb=error_cb,
            closed_cb=closed_cb,
        )
        await self.nc.subscribe(self.control_subject, cb=self.on_control)
        self.log_connection(
            "connection_up",
            nats_url=self.nats_url,
            server=getattr(self.nc, "connected_url", None),
        )
        await self.register()

    async def register(self) -> bool:
        async with self.registration_lock:
            if not self.nc or not self.nc.is_connected:
                return False
            request = {
                "interface_id": self.interface_id,
                "capability_card": self.capability_card(),
                "token_secret": self.token,
            }
            try:
                response = await self.nc.request(
                    "hub.register",
                    canonical_json(request),
                    timeout=10,
                )
                value = json.loads(response.data.decode("utf-8"))
            except Exception as error:
                self.registered = False
                self.log_connection(
                    "registration_transport_error",
                    error_type=type(error).__name__,
                )
                return False
            self.registered = bool(value.get("ok"))
            self.log_connection(
                "registration_complete",
                ok=self.registered,
                reason=value.get("reason"),
            )
            if self.registered:
                self.flush_complete.clear()
                asyncio.create_task(self.force_flush_outbox())
            return self.registered

    async def heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(self.heartbeat_s)
                if not self.nc or not self.nc.is_connected:
                    continue
                if not self.registered:
                    await self.register()
                    if not self.registered:
                        continue
                request = make_proof(
                    self.token,
                    {
                        "kind": "heartbeat",
                        "interface_id": self.interface_id,
                        "sent_at": now_iso(),
                    },
                )
                reply = await self.nc.request(
                    "hub.heartbeat", canonical_json(request), timeout=2
                )
                value = json.loads(reply.data.decode("utf-8"))
                if value.get("must_register"):
                    await self.register()
            except asyncio.CancelledError:
                return
            except Exception as error:
                self.log_connection(
                    "heartbeat_error", error_type=type(error).__name__
                )

    async def on_control(self, msg: Msg) -> None:
        try:
            request = json.loads(msg.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            if msg.reply:
                await msg.respond(b'{"ok":false,"reason":"malformed"}')
            return
        kind = request.get("kind")
        if kind == "probe":
            if request.get("interface_id") != self.interface_id:
                return
            response = {
                "kind": "probe_ack",
                "interface_id": self.interface_id,
                "nonce": request.get("nonce"),
            }
            response["proof"] = sign_value(self.token, response)
            if msg.reply:
                await msg.respond(canonical_json(response))
            return
        if kind == "echo":
            if self.suppress_echo:
                self.log_connection(
                    "echo_impersonation_test_suppressed",
                    challenge_id=request.get("challenge_id"),
                )
                return
            if request.get("interface_id") != self.interface_id:
                return
            response = {
                "kind": "echo_ack",
                "interface_id": self.interface_id,
                "challenge_id": request.get("challenge_id"),
                "marker": request.get("marker"),
            }
            response["proof"] = sign_value(self.token, response)
            if msg.reply:
                await msg.respond(canonical_json(response))
            return
        if kind == "dispatch":
            await self.on_dispatch(msg, request)
            return
        if msg.reply:
            await msg.respond(b'{"ok":false,"reason":"unknown_control_kind"}')

    async def on_dispatch(self, msg: Msg, request: dict[str, Any]) -> None:
        proof = request.get("proof")
        clean = dict(request)
        clean.pop("proof", None)
        if (
            request.get("interface_id") != self.interface_id
            or not isinstance(proof, str)
            or not verify_value(self.token, clean, proof)
        ):
            if msg.reply:
                await msg.respond(b'{"ok":false,"reason":"unauthorized"}')
            return
        envelope = request.get("envelope")
        if not isinstance(envelope, dict):
            if msg.reply:
                await msg.respond(b'{"ok":false,"reason":"invalid_envelope"}')
            return
        ok, reason, validated = validate_envelope(envelope)
        if not ok or validated is None:
            self.log_connection(
                "envelope_rejected",
                task_id=envelope.get("task_id"),
                reason=reason,
            )
            if msg.reply:
                await msg.respond(
                    canonical_json(
                        {"ok": False, "reason": f"envelope_invalid:{reason}"}
                    )
                )
            return
        target = validated.get("target") or {}
        if (
            target.get("interface_id")
            and target.get("interface_id") != self.interface_id
        ):
            if msg.reply:
                await msg.respond(b'{"ok":false,"reason":"wrong_target"}')
            return
        dispatch_id = str(request.get("dispatch_id", ""))
        # A duplicate confirms an already-durable task.  Resolve it before the
        # backfill and concurrency gates, which apply to new work.
        if validated["task_id"] in self.state.tasks:
            accepted, record = await self.state.accept_dispatch(
                validated, dispatch_id
            )
            if msg.reply:
                await msg.respond(
                    canonical_json(
                        {
                            "ok": True,
                            "duplicate": not accepted,
                            "execution_count": record.execution_count,
                        }
                    )
                )
            return
        if validated.get("cls") == "external":
            approval_ok, approval_reason = validate_approval_grant(
                validated, self.approval_secret
            )
            if not approval_ok:
                self.log_connection(
                    "approval_rejected",
                    task_id=validated.get("task_id"),
                    reason=approval_reason,
                    binding_sha256=approval_binding_sha256(validated),
                )
                if msg.reply:
                    await msg.respond(
                        canonical_json(
                            {
                                "ok": False,
                                "reason": f"envelope_invalid:{approval_reason}",
                                "retryable": False,
                            }
                        )
                    )
                return
            self.operation(
                str(validated["task_id"]),
                "approval_verify",
                str(validated["params"]["approval"]["credential_id"]),
                "valid",
            )
        if not self.flush_complete.is_set():
            if msg.reply:
                await msg.respond(
                    b'{"ok":false,"reason":"backfill_in_progress","retryable":true}'
                )
            return
        if len(self.inflight) >= self.max_concurrency:
            if msg.reply:
                await msg.respond(b'{"ok":false,"reason":"busy","retryable":true}')
            return
        accepted, record = await self.state.accept_dispatch(
            validated, dispatch_id
        )
        if msg.reply:
            await msg.respond(
                canonical_json(
                    {
                        "ok": True,
                        "duplicate": not accepted,
                        "execution_count": record.execution_count,
                    }
                )
            )
        if accepted:
            task_id = validated["task_id"]
            self.inflight.add(task_id)
            self.active_tasks[task_id] = asyncio.create_task(
                self.run_task(validated, dispatch_id)
            )

    async def outbox_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(0.5)
                if (
                    self.nc
                    and self.nc.is_connected
                    and self.registered
                    and self.flush_complete.is_set()
                ):
                    await self.flush_once()
            except asyncio.CancelledError:
                return
            except Exception as error:
                self.log_connection(
                    "outbox_loop_error", error_type=type(error).__name__
                )

    async def force_flush_outbox(self) -> None:
        if not self.nc or not self.registered:
            return
        self.flush_complete.clear()
        try:
            while self.state.pending_events() and not self.stop_event.is_set():
                delivered = await self.flush_once()
                if not self.nc.is_connected:
                    return
                if not delivered:
                    await asyncio.sleep(min(0.5, self.event_timeout_s))
            self.flush_complete.set()
        finally:
            if not self.state.pending_events():
                self.flush_complete.set()

    async def flush_once(self) -> bool:
        async with self.outbox_lock:
            return await self._flush_once_locked()

    async def _flush_once_locked(self) -> bool:
        if not self.nc or not self.nc.is_connected:
            return False
        progress = True
        for row in self.state.pending_events():
            if not self.nc.is_connected:
                return False
            event = {
                "task_id": row["task_id"],
                "trace_id": row["trace_id"],
                "seq": row["seq"],
                "fencing_generation": row["fencing_generation"],
                "kind": row["kind"],
                "payload": row["payload"],
                "created_at": row["created_at"],
            }
            request = make_proof(
                self.token,
                {
                    "kind": "event",
                    "interface_id": self.interface_id,
                    "event": event,
                    "sent_at": now_iso(),
                },
            )
            try:
                reply = await self.nc.request(
                    "hub.event",
                    canonical_json(request),
                    timeout=self.event_timeout_s,
                )
                response = json.loads(reply.data.decode("utf-8"))
                if response.get("ok"):
                    self.state.ack_event(row["event_id"], True, response)
                else:
                    self.state.ack_event(row["event_id"], False, response)
                    self.log_connection(
                        "event_rejected",
                        task_id=row["task_id"],
                        seq=row["seq"],
                        reason=response.get("reason"),
                    )
            except asyncio.TimeoutError:
                self.state.ack_event(
                    row["event_id"],
                    False,
                    {"reason": "event_timeout"},
                    timed_out=True,
                )
                self.log_connection(
                    "event_timeout",
                    task_id=row["task_id"],
                    seq=row["seq"],
                    timeout_s=self.event_timeout_s,
                )
                return False
            except Exception as error:
                self.log_connection(
                    "event_transport_pending",
                    task_id=row["task_id"],
                    seq=row["seq"],
                    error_type=type(error).__name__,
                )
                progress = False
                break
        return progress and not self.state.pending_events()

    async def claim(
        self, task_id: str, dispatch_id: str
    ) -> dict[str, Any] | None:
        if not self.nc:
            return None
        request = make_proof(
            self.token,
            {
                "kind": "claim",
                "interface_id": self.interface_id,
                "task_id": task_id,
                "dispatch_id": dispatch_id,
                "sent_at": now_iso(),
            },
        )
        try:
            # The event backlog must finish before any new task claim.
            if self.state.pending_events():
                await self.force_flush_outbox()
                if self.state.pending_events():
                    return None
            reply = await self.nc.request(
                "hub.claim", canonical_json(request), timeout=3
            )
            value = json.loads(reply.data.decode("utf-8"))
            return value if value.get("ok") else None
        except Exception as error:
            self.log_connection(
                "claim_error",
                task_id=task_id,
                error_type=type(error).__name__,
            )
            return None

    async def run_task(self, envelope: dict[str, Any], dispatch_id: str) -> None:
        task_id = envelope["task_id"]
        trace_id = envelope["trace_id"]
        generation = 0
        seq = 1
        heartbeat_task: asyncio.Task[Any] | None = None
        try:
            claim = await self.claim(task_id, dispatch_id)
            if not claim:
                self.log_connection("claim_rejected", task_id=task_id)
                return
            generation = int(claim["fencing_generation"])
            seq = int(claim["next_seq"])
            if envelope.get("cls") == "external":
                approval_ok, approval_reason = validate_approval_grant(
                    envelope, self.approval_secret
                )
                if not approval_ok:
                    raise RuntimeError(f"approval_recheck:{approval_reason}")
            await self.state.execution_started(task_id)
            self.operation(
                task_id,
                "accept_task",
                dispatch_id,
                "durable_inbox_record_written",
            )
            self.state.append_event(
                interface_id=self.interface_id,
                task_id=task_id,
                trace_id=trace_id,
                seq=seq,
                fencing_generation=generation,
                kind="accepted",
                payload={
                    "dispatch_id": dispatch_id,
                    "envelope_sha256": sha256_bytes(canonical_json(envelope)),
                },
            )
            seq += 1
            self.state.append_event(
                interface_id=self.interface_id,
                task_id=task_id,
                trace_id=trace_id,
                seq=seq,
                fencing_generation=generation,
                kind="claimed",
                payload={"lease_expires_at": claim.get("lease_expires_at")},
            )
            seq += 1
            self.state.append_event(
                interface_id=self.interface_id,
                task_id=task_id,
                trace_id=trace_id,
                seq=seq,
                fencing_generation=generation,
                kind="progress",
                payload={"stage": "running"},
            )
            seq += 1

            heartbeat_state = {"seq": seq}
            heartbeat_stop = asyncio.Event()

            async def heartbeat_task_events() -> None:
                while not heartbeat_stop.is_set():
                    try:
                        await asyncio.wait_for(
                            heartbeat_stop.wait(), timeout=self.task_heartbeat_s
                        )
                        return
                    except asyncio.TimeoutError:
                        current_seq = heartbeat_state["seq"]
                        self.state.append_event(
                            interface_id=self.interface_id,
                            task_id=task_id,
                            trace_id=trace_id,
                            seq=current_seq,
                            fencing_generation=generation,
                            kind="heartbeat",
                            payload={"task_alive": True},
                        )
                        heartbeat_state["seq"] = current_seq + 1

            heartbeat_task = asyncio.create_task(heartbeat_task_events())

            params = envelope.get("params") if isinstance(envelope.get("params"), dict) else {}
            test_mode = params.get("test_mode")
            content: bytes
            media_type = "text/plain; charset=utf-8"
            if self.executor == "verification-seat":
                content, media_type = await self.execute_verification_seat(envelope)
            elif test_mode in {"sleep", "late"}:
                if test_mode == "late":
                    sleep_s = (
                        float(params.get("late_after_s", 3))
                        if generation == 1
                        else float(params.get("retry_after_s", 0.25))
                    )
                else:
                    sleep_s = float(params.get("sleep_s", 1))
                self.operation(
                    task_id,
                    "test_sleep",
                    f"{sleep_s:.3f}s",
                    "started",
                )
                await asyncio.sleep(sleep_s)
                content = canonical_json(
                    {
                        "executor": "test_driver",
                        "test_mode": test_mode,
                        "task_id": task_id,
                        "generation": generation,
                        "completed_at": now_iso(),
                    }
                )
                media_type = "application/json"
                self.operation(
                    task_id,
                    "test_sleep",
                    f"{sleep_s:.3f}s",
                    "completed",
                )
            elif test_mode == "echo_instruction":
                content = envelope["instruction"].encode("utf-8")
                self.operation(
                    task_id,
                    "echo_instruction",
                    task_id,
                    "completed",
                )
            elif self.executor == "runtime":
                content, media_type = await self.execute_runtime(envelope)
            else:
                raise RuntimeError("test executor requires params.test_mode")

            heartbeat_stop.set()
            if heartbeat_task:
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
                heartbeat_task = None
            if heartbeat_state["seq"] > seq:
                seq = heartbeat_state["seq"]

            task_result_dir = self.results_dir / task_id
            task_result_dir.mkdir(parents=True, exist_ok=True)
            suffix = ".json" if "json" in media_type else ".txt"
            result_path = task_result_dir / f"result{suffix}"
            result_path.write_bytes(content)
            digest = sha256_file(result_path)
            self.operation(
                task_id,
                "write_result_file",
                str(result_path),
                f"bytes={result_path.stat().st_size};sha256={digest}",
            )
            pointer = {
                "uri": path_to_file_uri(result_path),
                "sha256": digest,
                "bytes": result_path.stat().st_size,
                "media_type": media_type,
            }
            self.state.append_event(
                interface_id=self.interface_id,
                task_id=task_id,
                trace_id=trace_id,
                seq=seq,
                fencing_generation=generation,
                kind="result_ready",
                payload={"pointer": pointer},
            )
            seq += 1
            self.state.append_event(
                interface_id=self.interface_id,
                task_id=task_id,
                trace_id=trace_id,
                seq=seq,
                fencing_generation=generation,
                kind="completed",
                payload={"execution_declared": True},
            )
            seq += 1
            await self.state.execution_finished(task_id, True, "result_pointer_created")
            self.log_connection(
                "task_execution_completed",
                task_id=task_id,
                fencing_generation=generation,
                next_seq=seq,
            )
        except Exception as error:
            if heartbeat_task:
                heartbeat_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat_task
            await self.state.execution_finished(
                task_id, False, f"{type(error).__name__}:{error}"[:300]
            )
            self.operation(
                task_id,
                "execute_task",
                task_id,
                f"failed:{type(error).__name__}",
            )
            self.state.append_event(
                interface_id=self.interface_id,
                task_id=task_id,
                trace_id=trace_id,
                seq=seq,
                fencing_generation=generation,
                kind="failed",
                payload={
                    "reason_category": "retryable",
                    "reason_code": f"interface_execution:{type(error).__name__}",
                    "detail": str(error)[:300],
                },
            )
        finally:
            self.inflight.discard(task_id)
            self.active_tasks.pop(task_id, None)
            asyncio.create_task(self.force_flush_outbox())

    async def execute_verification_seat(
        self, envelope: dict[str, Any]
    ) -> tuple[bytes, str]:
        """Run a verification-seat-style read-only verification or a lab external action."""

        task_id = envelope["task_id"]
        params = (
            envelope.get("params")
            if isinstance(envelope.get("params"), dict)
            else {}
        )
        mode = params.get("mode", "readonly_verify")
        if mode == "readonly_verify":
            if envelope.get("cls") != "readonly":
                raise RuntimeError("verification_seat_readonly_requires_readonly_cls")
            if len(envelope.get("attachments") or []) != 1:
                raise RuntimeError("verification_seat_readonly_requires_one_attachment")
            attachment = envelope["attachments"][0]
            source_path = file_uri_to_path(attachment["uri"])
            source_before = sha256_file(source_path)
            if source_before != attachment["sha256"]:
                raise RuntimeError("verification_seat_source_hash_mismatch")
            try:
                document = json.loads(source_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise RuntimeError(
                    f"verification_seat_source_invalid:{type(error).__name__}"
                ) from error
            if not isinstance(document, dict):
                raise RuntimeError("verification_seat_source_not_object")
            payload = document.get("payload")
            expected_payload = document.get("expected_payload_sha256")
            rules = document.get("rules")
            payload_hash = (
                sha256_bytes(canonical_json(payload))
                if isinstance(payload, dict)
                else ""
            )
            rule_results: dict[str, bool] = {}
            if isinstance(rules, list):
                for rule in rules:
                    if not isinstance(rule, dict) or not isinstance(
                        rule.get("name"), str
                    ):
                        continue
                    field = rule.get("field")
                    expected = rule.get("equals")
                    actual = payload.get(field) if isinstance(payload, dict) else None
                    rule_results[rule["name"]] = actual == expected
            deterministic_ok = (
                isinstance(payload, dict)
                and payload_hash == expected_payload
                and bool(rule_results)
                and all(rule_results.values())
            )
            source_after = sha256_file(source_path)
            self.operation(
                task_id,
                "readonly_read_source",
                str(source_path),
                f"unchanged={source_before == source_after};sha256={source_after}",
            )
            result = {
                "contract_version": CONTRACT_VERSION,
                "task_id": task_id,
                "role": "verification-seat",
                "capability": "readonly_verification",
                "instruction_received": envelope["instruction"],
                "source_uri": attachment["uri"],
                "source_sha256": source_after,
                "source_unchanged": source_before == source_after,
                "payload_sha256": payload_hash,
                "deterministic_checks": rule_results,
                "deterministic_result": "通过" if deterministic_ok else "驳回",
                "evidence_level": "E3-自证，未独立验证",
                "independent_verification": "not performed",
            }
            if not deterministic_ok:
                raise RuntimeError("verification_seat_deterministic_check_failed")
            return canonical_json(result), "application/json"

        if mode == "approval_action":
            if envelope.get("cls") != "external":
                raise RuntimeError("approval_action_requires_external_cls")
            approval_ok, approval_reason = validate_approval_grant(
                envelope, self.approval_secret
            )
            if not approval_ok:
                raise RuntimeError(f"approval_action_rejected:{approval_reason}")
            action_id = str(params["action_id"])
            action_path = self.data_dir / "external-actions" / f"{task_id}.json"
            action_path.parent.mkdir(parents=True, exist_ok=True)
            action_record = {
                "task_id": task_id,
                "trace_id": envelope["trace_id"],
                "action_id": action_id,
                "decision": envelope["params"]["approval"]["decision"],
                "credential_id": envelope["params"]["approval"]["credential_id"],
                "performed_at": now_iso(),
                "scope": "verification-seat-lab-only",
            }
            action_path.write_bytes(canonical_json(action_record))
            self.operation(
                task_id,
                "external_action",
                action_id,
                f"completed;path={action_path}",
            )
            return canonical_json(
                {
                    "task_id": task_id,
                    "action_id": action_id,
                    "action_result": "completed",
                    "scope": "verification-seat-lab-only",
                }
            ), "application/json"

        raise RuntimeError(f"verification_seat_mode_unknown:{mode}")

    async def execute_runtime(
        self, envelope: dict[str, Any]
    ) -> tuple[bytes, str]:
        task_id = envelope["task_id"]
        workdir = self.work_dir / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.results_dir / task_id / "runtime.stdout.jsonl"
        stderr_path = self.results_dir / task_id / "runtime.stderr.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.runtime_executable,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--json",
            "-C",
            str(workdir),
            envelope["instruction"],
        ]
        self.operation(
            task_id,
            "invoke_runtime_exec",
            self.runtime_executable,
            "started",
        )
        timeout_s = float(envelope["timeout_s"])
        completed = await asyncio.to_thread(
            subprocess.run,
            command,
            cwd=str(workdir),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if os.name == "nt"
                else 0
            ),
        )
        stdout_path.write_bytes(completed.stdout)
        stderr_path.write_bytes(completed.stderr)
        self.operation(
            task_id,
            "invoke_runtime_exec",
            self.runtime_executable,
            f"exit={completed.returncode};stdout_bytes={len(completed.stdout)};stderr_bytes={len(completed.stderr)}",
        )
        if completed.returncode != 0:
            raise RuntimeError(f"runtime_exec_exit_{completed.returncode}")
        return completed.stdout, "application/json"

    async def resume_durable_tasks(self) -> None:
        """Resume a locally accepted task only when its execution was not started.

        A task that already started is never re-executed automatically.  The hub
        lease/re-dispatch path handles that case through another generation.
        """

        for record in list(self.state.tasks.values()):
            if record.state != "accepted" or not record.envelope:
                continue
            task_id = record.task_id
            if task_id in self.active_tasks or task_id in self.inflight:
                continue
            dispatch = next(
                (
                    row
                    for row in reversed(self.state.dispatches.records())
                    if row.get("task_id") == task_id
                    and row.get("record") == "DISPATCH_ACCEPTED"
                ),
                None,
            )
            if dispatch:
                self.inflight.add(task_id)
                self.active_tasks[task_id] = asyncio.create_task(
                    self.run_task(record.envelope, dispatch["dispatch_id"])
                )

    async def run(self) -> None:
        await self.connect()
        heartbeat_task = None
        if not self.disable_heartbeat:
            heartbeat_task = asyncio.create_task(self.heartbeat_loop())
        outbox_task = asyncio.create_task(self.outbox_loop())
        await self.resume_durable_tasks()
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(
                    signal_name, lambda: self.stop_event.set()
                )
        print(
            json.dumps(
                {
                    "event": "interface_ready",
                    "pid": os.getpid(),
                    "interface_id": self.interface_id,
                    "control_subject": self.control_subject,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        await self.stop_event.wait()
        if heartbeat_task:
            heartbeat_task.cancel()
        outbox_task.cancel()
        if self.nc:
            await self.nc.drain()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--interface-id", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument(
        "--nats-url",
        default=os.environ.get(
            "VERIFICATION_SEAT_INTERFACE_V02_NATS_URL", "nats://127.0.0.1:43331"
        ),
    )
    parser.add_argument("--heartbeat-s", type=float, default=2.0)
    parser.add_argument("--task-heartbeat-s", type=float, default=5.0)
    parser.add_argument("--event-timeout-s", type=float, default=2.0)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--disable-heartbeat", action="store_true")
    parser.add_argument("--suppress-echo", action="store_true")
    parser.add_argument(
        "--executor", choices=["runtime", "test", "verification-seat"], default="test"
    )
    parser.add_argument(
        "--runtime-executable",
        default=os.environ.get("AGENT_RUNTIME_EXECUTABLE"),
    )
    args = parser.parse_args()
    if args.executor == "runtime" and not args.runtime_executable:
        parser.error(
            "runtime executor requires --runtime-executable or "
            "AGENT_RUNTIME_EXECUTABLE"
        )
    return args


def main() -> int:
    args = parse_args()
    process = InterfaceProcess(
        base=args.base.resolve(),
        interface_id=args.interface_id,
        token_file=args.token_file.resolve(),
        nats_url=args.nats_url,
        heartbeat_s=args.heartbeat_s,
        task_heartbeat_s=args.task_heartbeat_s,
        event_timeout_s=args.event_timeout_s,
        max_concurrency=args.max_concurrency,
        disable_heartbeat=args.disable_heartbeat,
        suppress_echo=args.suppress_echo,
        executor=args.executor,
        runtime_executable=args.runtime_executable,
    )
    try:
        asyncio.run(process.run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
