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
    canonical_json,
    make_proof,
    now_iso,
    path_to_file_uri,
    sha256_bytes,
    sha256_file,
    sign_value,
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
        work_root: Path | None = None,
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
        # work_root 给定时任务目录放在它下面（同一沙箱边界内）
        self.work_root = Path(work_root) if work_root else None
        self.work_dir = (self.work_root / ".uai-work") if self.work_root else (self.data_dir / "work")
        self.results_dir = self.data_dir / "results"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.control_subject = f"iface.{safe_id(interface_id)}.control"
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
        return {
            "interface_id": self.interface_id,
            "contract_version": CONTRACT_VERSION,
            "agent": {
                "vendor": "external-runtime",
                "runtime": "runtime-cli",
                "version": "runtime-provided",
            },
            "capabilities": [
                {
                    "name": "runtime_exec",
                    "description": "Execute a runtime CLI task and return a result pointer.",
                    "input_types": ["text/plain", "application/json"],
                    "output_types": ["application/json", "text/plain"],
                    "max_concurrency": self.max_concurrency,
                    "duration_class": "medium",
                }
            ],
            "sandbox": {
                "isolated": True,
                "type": "runtime-read-only-sandbox",
                "network": "disabled-by-task-policy",
                "code_execution_allowed": True,
            },
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
            name=f"interface-prototype-{safe_id(self.interface_id)}",
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
        sent = 0                     # 已发送心跳计数（用于限频日志）
        ticks = 0                    # 循环轮次（未连接时也计数，用于限频）
        while not self.stop_event.is_set():
            try:
                await asyncio.sleep(self.heartbeat_s)
                ticks += 1
                if not self.nc or not self.nc.is_connected:
                    if ticks % 30 == 1:
                        self.log_connection("heartbeat_skipped", reason="nats_not_connected")
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
                # 中枢重启/会话丢失时，注册状态在席位这边是过期的：
                # 只认 must_register 不够（旧中枢回的是 unauthorized），
                # 任何"不 ok"的回复都当成"重新注册一次"的信号。
                sent += 1
                if sent == 1 or sent % 30 == 0 or not value.get("ok"):
                    self.log_connection("heartbeat_reply", seq=sent,
                                         ok=bool(value.get("ok")),
                                         reason=value.get("reason"))
                if value.get("must_register") or not value.get("ok"):
                    self.registered = False
                    self.log_connection(
                        "re_register_requested",
                        reason=value.get("reason"),
                    )
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

    async def _flush_one_event(self, row: dict[str, Any]) -> bool:
        """投递**单条**事件。claim 用它在认领前冲掉本任务的积压，
        避免 force_flush_outbox() 的全局阻塞（codex 审代码第 1 条）。"""
        if not self.nc or not self.nc.is_connected:
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
        request = make_proof(self.token, {
            "kind": "event",
            "interface_id": self.interface_id,
            "event": event,
            "sent_at": now_iso(),
        })
        try:
            reply = await self.nc.request("hub.event", canonical_json(request),
                                         timeout=self.event_timeout_s)
            response = json.loads(reply.data.decode("utf-8"))
        except Exception:
            return False
        if response.get("ok"):
            self.state.ack_event(row["event_id"], True, response)
            return True
        reason = str(response.get("reason"))
        if reason in ("seq_gap", "task_not_found", "unknown_task", "task_terminal",
                     "fencing_stale", "fencing_rejected"):
            self.state.ack_event(row["event_id"], True,
                                 {"dropped": True, "reason": reason})
            self.log_connection("event_dropped", task_id=row["task_id"],
                                seq=row["seq"], reason=reason)
            return True
        self.state.ack_event(row["event_id"], False, response)
        return False


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
                    reason = str(response.get("reason"))
                    # hub 对 seq 与任务终态是权威。下面这些拒绝是**不会再变好**的：
                    # seq_gap = 本席重启后事件序号与 hub 记录脱节；task_not_found/终态 = 任务已不归本席。
                    # 若继续当 pending 重试，claim() 会因「backlog 未清空」永远 return None
                    # → 席位活着、锁在握、心跳正常，却永不认领任务（2026-11-18 实测：
                    #   一次 306s 超时把 codex 席位永久毒化）。
                    if reason in (
                        "seq_gap",
                        "task_not_found",
                        "unknown_task",
                        "task_terminal",
                        "fencing_stale",   # hub 实际返回的是这个词（codex 审出我写错成 fencing_rejected）
                        "fencing_rejected",
                    ):
                        self.state.ack_event(
                            row["event_id"],
                            True,
                            {
                                "dropped": True,
                                "reason": reason,
                                "expected_seq": response.get("expected_seq"),
                            },
                        )
                        self.log_connection(
                            "event_dropped",
                            task_id=row["task_id"],
                            seq=row["seq"],
                            reason=reason,
                            expected_seq=response.get("expected_seq"),
                        )
                        continue
                    self.state.ack_event(row["event_id"], False, response)
                    self.log_connection(
                        "event_rejected",
                        task_id=row["task_id"],
                        seq=row["seq"],
                        reason=reason,
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
            # 只冲**本任务**的积压，绝不调 force_flush_outbox()：
            # 那会循环到所有任务的事件清空，等于把全局阻塞加回来（codex 两轮审出）。
            pending_for_task = [
                row for row in self.state.pending_events()
                if row["task_id"] == task_id
            ][:20]
            for row in pending_for_task:
                if not await self._flush_one_event(row):
                    return None
            if any(row["task_id"] == task_id for row in self.state.pending_events()):
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
            if test_mode in {"sleep", "late"}:
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

            # I-2: 协商语义集成——检测 agent 返回的协商信号
            negotiation_response = None
            try:
                content_str = content.decode("utf-8", errors="replace")
                import re as _re
                m_reject = _re.search(r"\[NEGOTIATION:REJECT\]\s*(.+)", content_str)
                m_counter = _re.search(r"\[NEGOTIATION:COUNTER_OFFER\]\s*(.+)", content_str)
                m_delegate = _re.search(r"\[NEGOTIATION:DELEGATE:(\S+)\]", content_str)
                if m_reject:
                    negotiation_response = {"type": "reject", "reason": m_reject.group(1)[:200]}
                elif m_delegate:
                    negotiation_response = {"type": "delegate", "target": m_delegate.group(1)}
                elif m_counter:
                    negotiation_response = {"type": "counter_offer", "conditions": m_counter.group(1)[:200]}
            except Exception:
                pass

            if negotiation_response and negotiation_response["type"] in ("reject", "delegate"):
                # agent 拒绝/转派 → 发协商事件，不标记成功
                self.state.append_event(
                    interface_id=self.interface_id,
                    task_id=task_id,
                    trace_id=trace_id,
                    seq=seq,
                    fencing_generation=generation,
                    kind="negotiation_rejected",
                    payload={"negotiation": negotiation_response},
                )
                seq += 1
                await self.state.execution_finished(
                    task_id, False, f"negotiation_{negotiation_response['type']}"
                )
                self.operation(
                    task_id,
                    "negotiation_response",
                    task_id,
                    json.dumps(negotiation_response, ensure_ascii=False),
                )
                return

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


    def _roster(self) -> tuple[list[str], str]:
        """本机声明的席位名册。与监督器同源（seats.yml 的 seat 槽位），不新增协议。

        为什么要注入这段：派单只传 instruction 时，agent 完全不知道自己在哪个框架里、
        同伴是谁、自己这一手该交什么。实测（2026-11-18）问 dsh 席「你知道同伴吗」，
        它回答 UNKNOWN —— 这就是「面板 4 席运行中」与「agent 真协作」之间的差。
        """
        try:
            from uai_core import config as _cfg

            cfg = _cfg.load(None)
            ids = []
            for spec in (cfg.get("processes") or []):
                lock = str(spec.get("lock") or "")
                if lock.startswith("seat:"):
                    ids.append(lock.split(":", 1)[1])
            if ids:
                return sorted(set(ids)), "seats.yml"
        except Exception:
            pass
        return [self.interface_id], "fallback"

    def _preamble(self, envelope: dict[str, Any]) -> str:
        """派单前言：一屏之内交代「你是谁 / 同伴有谁 / 分工口径 / 回执格式」。"""
        seats, source = self._roster()
        peers = [s for s in seats if s != self.interface_id]
        peer_text = ", ".join(peers) if peers else "（本机未声明其他席位）"
        cls = str(envelope.get("cls") or "readonly")
        # 必须**单行**：dsh/codex/rivet 的 CLI 只把首个参数当 prompt，
        # 带换行的前言会让它们只看到第一行（实测：三席都回「没有任务」）。
        return (
            "【UAI 协作上下文】你是 UAI 框架的 " + self.interface_id + " 席。"
            "同一框架内声明的其他席位：" + peer_text + "（来源：" + source + "）。"
            "分工口径（标准协作模式四件套）：实现 → 审代码 → 实测验 → 回读对账；谁实现谁不自审。"
            "本次派单类别：" + cls + "；只做被要求的那一件事，不越权修改他人产物。"
            "回执只输出结果正文（结论 + 依据），不写文件，不复述本段上下文。"
        )

    async def execute_runtime(
        self, envelope: dict[str, Any]
    ) -> tuple[bytes, str]:
        task_id = envelope["task_id"]
        workdir = self.work_dir / task_id
        workdir.mkdir(parents=True, exist_ok=True)
        stdout_path = self.results_dir / task_id / "runtime.stdout.jsonl"
        stderr_path = self.results_dir / task_id / "runtime.stderr.log"
        stdout_path.parent.mkdir(parents=True, exist_ok=True)
        runtime_cmd = [self.runtime_executable]
        if self.runtime_executable.lower().endswith(".py"):
            # Windows 无法直接 spawn .py；Python 解释器包装（P0 外部 runtime 接入实测踩过）
            runtime_cmd = [sys.executable, self.runtime_executable]
        command = [
            *runtime_cmd,
            "exec",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--ephemeral",
            "--json",
            "-C",
            str(workdir),
            envelope["instruction"] + " " + self._preamble(envelope),
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
        default=os.environ.get("INTERFACE_PROTO_NATS_URL", "nats://127.0.0.1:43321"),
    )
    parser.add_argument("--heartbeat-s", type=float, default=2.0)
    parser.add_argument("--task-heartbeat-s", type=float, default=5.0)
    parser.add_argument("--event-timeout-s", type=float, default=2.0)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--disable-heartbeat", action="store_true")
    parser.add_argument("--suppress-echo", action="store_true")
    parser.add_argument(
        "--work-root",
        type=Path,
        default=None,
        help="任务工作目录根：给了就在 <root>/.uai-work/<task_id> 下跑，"
        "使受限 runtime（rivet 沙箱）能访问该根内的仓库；不给则沿用 base 下空目录",
    )
    parser.add_argument("--executor", choices=["runtime", "test"], default="test")
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


EXIT_SEAT_ALREADY_RUNNING = 3


def _acquire_seat_singleton(base: Path, interface_id: str):
    """同一席位 id 只允许一个进程。

    中枢侧已有单例锁（UAI-13），席位侧原来没有：手动再起一个 interface.py 就能和
    常驻那个并存，而中枢只按 interface_id 认会话——它看不出身后站了两个写者。
    """
    import sys as _sys

    stage2 = Path(__file__).resolve().parent.parent / "stage2"
    if str(stage2) not in _sys.path:
        _sys.path.insert(0, str(stage2))
    try:
        from hub_singleton import HubSingleton
    except Exception as exc:
        return None, {"reason": "singleton_unavailable:%s" % str(exc)[:100]}

    guard = HubSingleton(
        Path(base) / "data" / ("seat-%s.lock" % interface_id),
        namespace="uai-seat:%s" % interface_id,
        ttl_s=float(os.environ.get("UAI_SEAT_SINGLETON_TTL", "600")),
    )
    ok, info = guard.acquire()
    return (guard, info) if ok else (None, info)


def _renew_seat_lock_forever(guard) -> None:
    """守护线程续期。TTL 只用于告警；真裁决在 OS 独占锁上。"""
    import threading
    import time as _time

    def _loop() -> None:
        while True:
            _time.sleep(max(5.0, float(guard.ttl_s) / 3.0))
            try:
                guard.heartbeat()
            except Exception:
                return

    threading.Thread(target=_loop, name="seat-lock-heartbeat", daemon=True).start()


def main() -> int:
    args = parse_args()
    base = args.base.resolve()
    guard, info = _acquire_seat_singleton(base, args.interface_id)
    if guard is None:
        # 第二个同 id 席位**不许**静默共存：中枢会把一个 id 当成两个写者。
        print(
            json.dumps(
                {
                    "event": "seat_singleton_denied",
                    "interface_id": args.interface_id,
                    "reason": info.get("reason"),
                    "holder": info.get("holder"),
                    "pid": os.getpid(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return EXIT_SEAT_ALREADY_RUNNING
    _renew_seat_lock_forever(guard)
    import atexit
    atexit.register(guard.release)
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
        work_root=args.work_root,
    )
    try:
        asyncio.run(process.run())
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
