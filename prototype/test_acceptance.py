#!/usr/bin/env python3
"""Process-level acceptance harness for all ten items in specification §15."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

import nats

from core import (
    atomic_write_json,
    file_uri_to_path,
    now_iso,
    read_jsonl,
    sha256_file,
)
from lab_client import LabClient, build_envelope


TERMINAL = {"SUCCEEDED", "FAILED", "TIMED_OUT", "CANCELLED"}
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class ProcessHandle:
    def __init__(
        self,
        process: subprocess.Popen[Any],
        command: list[str],
        out_path: Path,
        err_path: Path,
        marker: str | None = None,
    ):
        self.process = process
        self.command = command
        self.out_path = out_path
        self.err_path = err_path
        self.marker = marker

    def stop(self) -> None:
        if self.process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        if self.marker:
            escaped = self.marker.replace("'", "''")
            script = rf"""
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -eq 'python.exe' -and
  $_.CommandLine -like '*{escaped}*'
}} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
"""
            subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
        with contextlib.suppress(Exception):
            self.process.wait(timeout=5)


class AcceptanceRunner:
    def __init__(self, base: Path, nats_url: str):
        self.base = base
        self.nats_url = nats_url
        self.run_id = time.strftime("%Y%m%dT%H%M%S", time.localtime()) + "-" + uuid.uuid4().hex[:6]
        self.log_dir = base / "logs" / "acceptance" / self.run_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.evidence_dir = base / "evidence"
        self.results_path = self.evidence_dir / "acceptance-results.json"
        self.transcript_path = self.log_dir / "acceptance-transcript.log"
        self.results: dict[str, dict[str, Any]] = {}
        self.clients: list[LabClient] = []
        self.processes: list[ProcessHandle] = []
        self.interface_ids: dict[str, str] = {}
        self.task_ids: dict[str, str] = {}

    def log(self, message: str) -> None:
        line = f"{now_iso()} {message}"
        with self.transcript_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        print(line, flush=True)

    def provision(self, role: str) -> tuple[str, Path]:
        interface_id = f"accept-{self.run_id}-{role}"
        token_path = self.base / ".lab-tokens" / f"{interface_id}.token"
        command = [
            sys.executable,
            str(self.base / "provision_tokens.py"),
            "--base",
            str(self.base),
            "--interface-id",
            interface_id,
        ]
        completed = subprocess.run(
            command,
            cwd=str(self.base),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=CREATE_NO_WINDOW,
            timeout=20,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"token provisioning failed: {completed.stderr[-500:]}")
        if not token_path.is_file():
            raise RuntimeError("token provisioning did not create the lab token file")
        self.interface_ids[role] = interface_id
        return interface_id, token_path

    def start_interface(
        self,
        role: str,
        *,
        heartbeat_s: float = 0.5,
        task_heartbeat_s: float = 2.0,
        event_timeout_s: float = 1.0,
        max_concurrency: int = 2,
        disable_heartbeat: bool = False,
        suppress_echo: bool = False,
        executor: str = "test",
    ) -> tuple[str, ProcessHandle]:
        interface_id, token_path = self.provision(role)
        command = [
            sys.executable,
            str(self.base / "interface.py"),
            "--base",
            str(self.base),
            "--interface-id",
            interface_id,
            "--token-file",
            str(token_path),
            "--nats-url",
            self.nats_url,
            "--heartbeat-s",
            str(heartbeat_s),
            "--task-heartbeat-s",
            str(task_heartbeat_s),
            "--event-timeout-s",
            str(event_timeout_s),
            "--max-concurrency",
            str(max_concurrency),
            "--executor",
            executor,
        ]
        if disable_heartbeat:
            command.append("--disable-heartbeat")
        if suppress_echo:
            command.append("--suppress-echo")
        out_path = self.log_dir / f"{role}.out.log"
        err_path = self.log_dir / f"{role}.err.log"
        out_handle = out_path.open("wb")
        err_handle = err_path.open("wb")
        process = subprocess.Popen(
            command,
            cwd=str(self.base),
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=err_handle,
            creationflags=CREATE_NO_WINDOW,
        )
        out_handle.close()
        err_handle.close()
        handle = ProcessHandle(
            process, command, out_path, err_path, marker=interface_id
        )
        self.processes.append(handle)
        return interface_id, handle

    async def wait_registration(
        self,
        interface_id: str,
        *,
        expected_ok: bool = True,
        timeout_s: float = 15,
    ) -> dict[str, Any]:
        path = (
            self.base
            / "runtime"
            / "interfaces"
            / interface_id
            / "connection.jsonl"
        )

        def find() -> dict[str, Any] | None:
            if not path.exists():
                return None
            for row in reversed(read_jsonl(path)):
                if row.get("msg") != "registration_complete":
                    continue
                if bool(row.get("ok")) == expected_ok:
                    return row
            return None

        return await self.wait_until(
            find,
            lambda value: value is not None,
            timeout_s=timeout_s,
            label=f"registration {interface_id} ok={expected_ok}",
        ) or {}

    async def wait_until(
        self,
        getter: Callable[[], Any] | Callable[[], Awaitable[Any]],
        predicate: Callable[[Any], bool],
        *,
        timeout_s: float,
        interval_s: float = 0.15,
        label: str,
    ) -> Any:
        deadline = time.monotonic() + timeout_s
        latest: Any = None
        while time.monotonic() < deadline:
            value = getter()
            if asyncio.iscoroutine(value):
                latest = await value
            else:
                latest = value
            if predicate(latest):
                return latest
            await asyncio.sleep(interval_s)
        raise TimeoutError(f"timeout waiting for {label}; latest={latest!r}")

    async def client(self) -> LabClient:
        value = LabClient(self.nats_url)
        await value.connect()
        self.clients.append(value)
        return value

    def ledger(self) -> list[dict[str, Any]]:
        return read_jsonl(self.base / "data" / "ledger.jsonl")

    def ledger_for(self, record_type: str, task_id: str | None = None) -> list[dict[str, Any]]:
        return [
            row
            for row in self.ledger()
            if row.get("record") == record_type
            and (task_id is None or row.get("task_id") == task_id)
        ]

    async def wait_ledger(
        self,
        record_type: str,
        predicate: Callable[[dict[str, Any]], bool],
        *,
        timeout_s: float,
        label: str,
    ) -> dict[str, Any]:
        def matching() -> list[dict[str, Any]]:
            return [row for row in self.ledger() if row.get("record") == record_type and predicate(row)]

        rows = await self.wait_until(
            matching,
            bool,
            timeout_s=timeout_s,
            label=label,
        )
        return rows[0]

    def interface_dir(self, interface_id: str) -> Path:
        return self.base / "runtime" / "interfaces" / interface_id

    def pending_events(self, interface_id: str) -> list[dict[str, Any]]:
        data_dir = self.interface_dir(interface_id)
        events = read_jsonl(data_dir / "outbox.jsonl")
        acks = read_jsonl(data_dir / "outbox-acks.jsonl")
        acked = {
            row["event_id"]
            for row in acks
            if row.get("record") == "ACK" and row.get("ok") is True
        }
        return [
            row
            for row in events
            if row.get("record") == "EVENT" and row.get("event_id") not in acked
        ]

    def result(self, item: int, name: str, passed: bool, command: str, summary: str, evidence: list[str], output: dict[str, Any] | None = None) -> None:
        value = {
            "item": item,
            "name": name,
            "verdict": "通过" if passed else "不通过",
            "evidence_level": "E3-自证，未独立验证",
            "command": command,
            "exit_code": 0 if passed else 1,
            "summary": summary,
            "evidence_paths": evidence,
            "output": output or {},
            "checked_at": now_iso(),
        }
        self.results[str(item)] = value
        self.log(
            f"CHECK item={item} name={name} verdict={value['verdict']} exit_code={value['exit_code']} summary={summary}"
        )
        atomic_write_json(
            self.results_path,
            {
                "run_id": self.run_id,
                "self_test": True,
                "independent_verification": "not performed",
                "results": self.results,
            },
        )

    async def run_case(
        self,
        item: int,
        name: str,
        command: str,
        function: Callable[[], Awaitable[tuple[bool, str, list[str], dict[str, Any]]]],
    ) -> None:
        self.log(f"START item={item} {name}")
        try:
            passed, summary, evidence, output = await function()
        except Exception as error:
            passed = False
            summary = f"{type(error).__name__}: {error}"[:800]
            evidence = [str(self.transcript_path)]
            output = {"exception_type": type(error).__name__}
            self.log(f"ERROR item={item} {type(error).__name__}: {error}")
        self.result(item, name, passed, command, summary, evidence, output)

    async def cleanup_interfaces(self) -> None:
        for handle in self.processes:
            handle.stop()
        self.processes.clear()
        escaped_base = str(self.base).replace("'", "''")
        script = rf"""
$base = '{escaped_base}'
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -eq 'python.exe' -and
  ($_.CommandLine -like "*$base\interface.py*" -or
   $_.CommandLine -like "*$base\fake_responder.py*")
}} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }}
"""
        subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        for client in self.clients:
            with contextlib.suppress(Exception):
                await client.close()
        self.clients.clear()

    async def test_01_stale(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        interface_id, handle = self.start_interface(
            "stale",
            heartbeat_s=0.6,
            disable_heartbeat=True,
        )
        connection_path = self.interface_dir(interface_id) / "connection.jsonl"
        await self.wait_registration(interface_id, expected_ok=True)
        stale = await self.wait_ledger(
            "INTERFACE_STALE",
            lambda row: row.get("interface_id") == interface_id,
            timeout_s=5,
            label="interface stale",
        )
        envelope = build_envelope(
            instruction="stale dispatch guard",
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "echo_instruction"},
            timeout_s=10,
        )
        submitted = await client.submit(envelope)
        task_id = submitted["task_id"]
        self.task_ids["stale"] = task_id
        await asyncio.sleep(1.0)
        state = await client.state(task_id)
        dispatch_rows = self.ledger_for("TASK_STATE", task_id)
        dispatched = any(row.get("state") == "DISPATCHED" for row in dispatch_rows)
        handle.stop()
        passed = bool(submitted.get("ok")) and state["state"] == "QUEUED" and not dispatched
        return (
            passed,
            f"interface reached stale after 3 missed heartbeats; queued task remained {state['state']}; dispatched={dispatched}",
            [str(connection_path), str(self.base / "data" / "ledger.jsonl"), str(self.transcript_path)],
            {"interface_id": interface_id, "task_id": task_id, "stale_record": stale, "state": state["state"], "dispatched": dispatched},
        )

    async def test_02_impersonation(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        interface_id, token_path = self.provision("impersonated")
        fake_log = self.log_dir / "fake-responder.log"
        command = [
            sys.executable,
            str(self.base / "fake_responder.py"),
            "--interface-id",
            interface_id,
            "--nats-url",
            self.nats_url,
            "--log",
            str(fake_log),
        ]
        fake_stdout = self.log_dir / "fake-responder.stdout.log"
        fake_stderr = self.log_dir / "fake-responder.stderr.log"
        stdout_handle = fake_stdout.open("wb")
        stderr_handle = fake_stderr.open("wb")
        fake = subprocess.Popen(
            command,
            cwd=str(self.base),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            creationflags=CREATE_NO_WINDOW,
        )
        stdout_handle.close()
        stderr_handle.close()
        fake_handle = ProcessHandle(
            fake, command, fake_stdout, fake_stderr, marker=interface_id
        )
        self.processes.append(fake_handle)
        await self.wait_until(
            lambda: fake_stdout.exists() and "fake_responder_ready" in fake_stdout.read_text(encoding="utf-8", errors="replace"),
            bool,
            timeout_s=8,
            label="fake responder ready",
        )
        interface_command = [
            sys.executable,
            str(self.base / "interface.py"),
            "--base",
            str(self.base),
            "--interface-id",
            interface_id,
            "--token-file",
            str(token_path),
            "--nats-url",
            self.nats_url,
            "--heartbeat-s",
            "0.6",
            "--task-heartbeat-s",
            "2",
            "--event-timeout-s",
            "1",
            "--max-concurrency",
            "1",
            "--executor",
            "test",
            "--suppress-echo",
        ]
        normal_stdout = self.log_dir / "impersonated-interface.stdout.log"
        normal_stderr = self.log_dir / "impersonated-interface.stderr.log"
        out_handle = normal_stdout.open("wb")
        err_handle = normal_stderr.open("wb")
        process = subprocess.Popen(
            interface_command,
            cwd=str(self.base),
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=err_handle,
            creationflags=CREATE_NO_WINDOW,
        )
        out_handle.close()
        err_handle.close()
        normal_handle = ProcessHandle(
            process,
            interface_command,
            normal_stdout,
            normal_stderr,
            marker=interface_id,
        )
        self.processes.append(normal_handle)
        failed_registration = await self.wait_registration(
            interface_id, expected_ok=False, timeout_s=12
        )
        rejected = await self.wait_ledger(
            "REGISTRATION_REJECTED",
            lambda row: row.get("interface_id") == interface_id
            and str(row.get("reason", "")).startswith("echo_failed:"),
            timeout_s=5,
            label="impersonated registration rejected",
        )
        forged = await self.wait_until(
            lambda: read_jsonl(fake_log),
            lambda rows: any(row.get("msg") == "forged_echo_sent" for row in rows),
            timeout_s=5,
            label="forged echo record",
        )
        active_rows = [
            row
            for row in self.ledger_for("INTERFACE_ACTIVE")
            if row.get("interface_id") == interface_id
        ]
        normal_handle.stop()
        fake_handle.stop()
        passed = bool(rejected) and bool(active_rows) is False and any(
            row.get("msg") == "forged_echo_sent" for row in forged
        )
        return (
            passed,
            f"separate process echoed marker but failed token proof; registration rejected; active_rows={len(active_rows)}",
            [str(fake_log), str(self.interface_dir(interface_id) / "connection.jsonl"), str(self.base / "data" / "ledger.jsonl")],
            {"interface_id": interface_id, "registration": failed_registration, "rejection": rejected},
        )

    async def test_03_exact_instruction(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        interface_id, handle = self.start_interface("exact")
        await self.wait_registration(interface_id)
        instruction = (
            "第一行：双引号\"X\"，单引号'Y'\n"
            "第二行：反斜杠\\ 和 C:\\temp\\sample.txt\n"
            "```python\n"
            "print(\"hello\")\n"
            "```\n"
            "末行保留"
        )
        envelope = build_envelope(
            instruction=instruction,
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "echo_instruction"},
            timeout_s=10,
        )
        response = await client.submit(envelope)
        state = await client.wait_state(response["task_id"], TERMINAL, timeout_s=15)
        self.task_ids["exact"] = response["task_id"]
        pointer = state.get("result_pointer") or {}
        result_path = file_uri_to_path(pointer["uri"])
        received = result_path.read_bytes()
        accepted_rows = [
            row
            for row in self.ledger_for("TASK_ACCEPTED")
            if row.get("task_id") == response["task_id"]
            or (row.get("envelope") or {}).get("task_id") == response["task_id"]
        ]
        if not accepted_rows:
            raise AssertionError("central acceptance ledger row is missing")
        accepted = accepted_rows[0]
        central_instruction = accepted["envelope"]["instruction"]
        local_inbox = read_jsonl(self.interface_dir(interface_id) / "inbox.jsonl")
        local_rows = [
            row
            for row in local_inbox
            if row.get("record") == "DISPATCH_ACCEPTED"
            and row.get("task_id") == response["task_id"]
        ]
        if not local_rows:
            raise AssertionError("local durable inbox row is missing")
        local_envelope = local_rows[0]["envelope"]
        handle.stop()
        passed = (
            state["state"] == "SUCCEEDED"
            and received == instruction.encode("utf-8")
            and central_instruction == instruction
            and local_envelope["instruction"] == instruction
        )
        return (
            passed,
            f"exact byte equality at submitted/central/local/result={passed}; bytes={len(received)}",
            [str(self.base / "data" / "ledger.jsonl"), str(self.interface_dir(interface_id) / "inbox.jsonl"), str(result_path)],
            {"task_id": response["task_id"], "result_bytes": len(received), "sha256": pointer.get("sha256")},
        )

    async def test_04_outage_five_minutes(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        interface_id, handle = self.start_interface(
            "offline",
            heartbeat_s=0.5,
            task_heartbeat_s=2,
            event_timeout_s=0.7,
        )
        await self.wait_registration(interface_id)
        envelope = build_envelope(
            instruction="five-minute offline durability",
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "sleep", "sleep_s": 12, "_lease_seconds": 600},
            timeout_s=480,
        )
        response = await client.submit(envelope)
        task_id = response["task_id"]
        self.task_ids["offline"] = task_id
        await client.wait_state(task_id, {"CLAIMED", "RUNNING"}, timeout_s=10)
        nats_pid = self.nats_listener_pid()
        self.verify_nats_pid(nats_pid)
        outage_started_monotonic = time.monotonic()
        outage_started = now_iso()
        self.stop_nats(nats_pid)
        self.log(f"OUTAGE start={outage_started} pid={nats_pid}")
        await asyncio.sleep(12)
        pending_during = self.pending_events(interface_id)
        if not pending_during:
            local_events = read_jsonl(self.interface_dir(interface_id) / "outbox.jsonl")
            raise AssertionError(
                f"no pending outbox records after broker stop; local_event_rows={len(local_events)}"
            )
        while time.monotonic() - outage_started_monotonic < 300:
            await asyncio.sleep(min(30, 300 - (time.monotonic() - outage_started_monotonic)))
            self.log(
                f"OUTAGE waiting elapsed_s={time.monotonic() - outage_started_monotonic:.1f} pending={len(self.pending_events(interface_id))}"
            )
        self.start_nats()
        outage_ended = now_iso()
        outage_seconds = time.monotonic() - outage_started_monotonic
        self.log(f"OUTAGE end={outage_ended} elapsed_s={outage_seconds:.1f}")
        state = await client.wait_state(task_id, {"SUCCEEDED"}, timeout_s=90)
        await self.wait_until(
            lambda: self.pending_events(interface_id),
            lambda pending: len(pending) == 0,
            timeout_s=60,
            label="offline outbox backfill",
        )
        local_events = [
            row
            for row in read_jsonl(self.interface_dir(interface_id) / "outbox.jsonl")
            if row.get("record") == "EVENT" and row.get("task_id") == task_id
        ]
        central_events = self.ledger_for("TASK_EVENT", task_id)
        local_seqs = sorted(int(row["seq"]) for row in local_events)
        central_seqs = sorted(int(row["seq"]) for row in central_events)
        connection_rows = read_jsonl(self.interface_dir(interface_id) / "connection.jsonl")
        hub_connection_rows = read_jsonl(self.base / "logs" / "hub-connection.jsonl")
        interface_recovered = any(row.get("msg") == "connection_up" for row in connection_rows[-20:])
        hub_recovered = any(row.get("msg") == "connection_up" for row in hub_connection_rows[-20:])
        handle.stop()
        passed = (
            outage_seconds >= 300
            and state["state"] == "SUCCEEDED"
            and local_seqs == central_seqs
            and not self.pending_events(interface_id)
            and interface_recovered
            and hub_recovered
        )
        return (
            passed,
            f"broker down for {outage_seconds:.1f}s; pending during outage={len(pending_during)}; final seqs local={local_seqs} central={central_seqs}; pending_final=0",
            [
                str(self.interface_dir(interface_id) / "outbox.jsonl"),
                str(self.interface_dir(interface_id) / "outbox-acks.jsonl"),
                str(self.interface_dir(interface_id) / "connection.jsonl"),
                str(self.base / "logs" / "hub-connection.jsonl"),
                str(self.base / "data" / "ledger.jsonl"),
            ],
            {
                "task_id": task_id,
                "outage_started": outage_started,
                "outage_ended": outage_ended,
                "outage_seconds": round(outage_seconds, 3),
                "pending_during_outage": len(pending_during),
                "local_seqs": local_seqs,
                "central_seqs": central_seqs,
            },
        )

    async def test_05_duplicate_delivery(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        interface_id, handle = self.start_interface("duplicate")
        await self.wait_registration(interface_id)
        envelope = build_envelope(
            instruction="duplicate delivery",
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "sleep", "sleep_s": 0.6},
            timeout_s=10,
        )
        response = await client.submit(envelope)
        task_id = response["task_id"]
        self.task_ids["duplicate"] = task_id
        await client.wait_state(task_id, TERMINAL, timeout_s=15)
        for _ in range(3):
            await client.debug({"action": "redispatch", "task_id": task_id})
        await asyncio.sleep(0.8)
        inbox_rows = read_jsonl(self.interface_dir(interface_id) / "inbox.jsonl")
        dispatch_rows = read_jsonl(self.interface_dir(interface_id) / "dispatch.jsonl")
        executions = [
            row
            for row in inbox_rows
            if row.get("record") == "EXECUTION_STARTED" and row.get("task_id") == task_id
        ]
        duplicate_rows = [
            row
            for row in dispatch_rows
            if row.get("record") == "DISPATCH_DUPLICATE" and row.get("task_id") == task_id
        ]
        state = await client.state(task_id)
        handle.stop()
        passed = len(executions) == 1 and len(duplicate_rows) >= 3 and state["state"] == "SUCCEEDED"
        return (
            passed,
            f"three lab redispatches produced duplicate_acknowledgements={len(duplicate_rows)}; execution_started={len(executions)}",
            [str(self.interface_dir(interface_id) / "inbox.jsonl"), str(self.interface_dir(interface_id) / "dispatch.jsonl")],
            {"task_id": task_id, "execution_count": len(executions), "duplicate_count": len(duplicate_rows)},
        )

    async def test_06_lease_fencing(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        old_id, old_handle = self.start_interface(
            "lease-a-old",
            heartbeat_s=0.5,
            task_heartbeat_s=5,
            max_concurrency=2,
        )
        new_id, new_handle = self.start_interface(
            "lease-b-new",
            heartbeat_s=0.5,
            task_heartbeat_s=1,
            max_concurrency=2,
        )
        await self.wait_registration(old_id)
        await self.wait_registration(new_id)
        # Let any interfaces from earlier cases cross the three-heartbeat stale
        # threshold before capability-based routing chooses the two lease workers.
        await asyncio.sleep(2.0)
        envelope = build_envelope(
            instruction="lease recovery and late result",
            capability="runtime_exec",
            params={"test_mode": "late", "late_after_s": 3, "_lease_seconds": 1.1},
            timeout_s=10,
        )
        response = await client.submit(envelope)
        task_id = response["task_id"]
        self.task_ids["lease"] = task_id
        lease_expired = await self.wait_ledger(
            "LEASE_EXPIRED",
            lambda row: row.get("task_id") == task_id,
            timeout_s=10,
            label="lease expiry",
        )
        state = await client.wait_state(task_id, {"SUCCEEDED"}, timeout_s=15)
        rejected = await self.wait_ledger(
            "EVENT_REJECTED",
            lambda row: row.get("task_id") == task_id
            and row.get("reason") == "fencing_stale",
            timeout_s=10,
            label="late event fenced",
        )
        old_handle.stop()
        new_handle.stop()
        state_rows = self.ledger_for("TASK_STATE", task_id)
        generations = sorted(
            {
                int(row["fencing_generation"])
                for row in state_rows
                if row.get("state") == "CLAIMED"
            }
        )
        passed = (
            state["state"] == "SUCCEEDED"
            and bool(lease_expired)
            and bool(rejected)
            and generations == [1, 2]
        )
        return (
            passed,
            f"lease reclaimed and reassigned with generations={generations}; old late result rejected reason={rejected.get('reason')}",
            [str(self.base / "data" / "ledger.jsonl"), str(self.transcript_path)],
            {"task_id": task_id, "generations": generations, "lease_expired": lease_expired, "late_rejection": rejected},
        )

    async def test_07_resource_and_concurrency(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        interface_id, handle = self.start_interface(
            "resources",
            heartbeat_s=0.5,
            task_heartbeat_s=2,
            max_concurrency=4,
        )
        await self.wait_registration(interface_id)

        async def submit(instruction: str, resource: str, sleep_s: float) -> str:
            response = await client.submit(
                build_envelope(
                    instruction=instruction,
                    interface_id=interface_id,
                    capability=None,
                    resource_key=resource,
                    params={"test_mode": "sleep", "sleep_s": sleep_s},
                    timeout_s=15,
                )
            )
            return response["task_id"]

        a1 = await submit("resource A first", "resource-A", 2.0)
        await client.wait_state(a1, {"RUNNING"}, timeout_s=8)
        a2 = await submit("resource A second", "resource-A", 1.0)
        b1 = await submit("resource B parallel", "resource-B", 2.0)
        await asyncio.gather(
            client.wait_state(a1, TERMINAL, timeout_s=15),
            client.wait_state(a2, TERMINAL, timeout_s=15),
            client.wait_state(b1, TERMINAL, timeout_s=15),
        )
        ops = read_jsonl(self.interface_dir(interface_id) / "operations.jsonl")

        def marker(task_id: str, result: str) -> dict[str, Any]:
            rows = [
                row
                for row in ops
                if row.get("task_id") == task_id
                and row.get("action") == "test_sleep"
                and row.get("result") == result
            ]
            if not rows:
                raise AssertionError(
                    f"missing operation marker task_id={task_id} result={result}"
                )
            return rows[0]

        a1s, a1e = marker(a1, "started"), marker(a1, "completed")
        a2s, b1s = marker(a2, "started"), marker(b1, "started")
        a2_after_a1 = a2s["ts"] >= a1e["ts"]
        b_overlap = b1s["ts"] < a1e["ts"]
        handle.stop()
        passed = a2_after_a1 and b_overlap
        return (
            passed,
            f"same resource ordered={a2_after_a1}; cross-resource overlap={b_overlap}; A1_start={a1s['ts']} A1_end={a1e['ts']} A2_start={a2s['ts']} B_start={b1s['ts']}",
            [str(self.interface_dir(interface_id) / "operations.jsonl"), str(self.base / "data" / "ledger.jsonl")],
            {"task_ids": {"a1": a1, "a2": a2, "b1": b1}, "same_resource_serial": a2_after_a1, "cross_resource_parallel": b_overlap},
        )

    async def test_08_timeout_layers(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        started = time.monotonic()
        connection_error = None

        async def ignore_connect_error(_error: Exception) -> None:
            return None

        try:
            async with asyncio.timeout(1.0):
                await nats.connect(
                    "nats://127.0.0.1:43329",
                    connect_timeout=0.25,
                    max_reconnect_attempts=0,
                    allow_reconnect=False,
                    error_cb=ignore_connect_error,
                )
        except Exception as error:
            connection_error = error
        connection_elapsed = time.monotonic() - started

        client = await self.client()
        event_id, event_handle = self.start_interface(
            "event-timeout",
            heartbeat_s=0.5,
            event_timeout_s=0.25,
        )
        await self.wait_registration(event_id)
        await client.debug({"action": "pause_events", "seconds": 2.0})
        event_task = (
            await client.submit(
                build_envelope(
                    instruction="event timeout layer",
                    interface_id=event_id,
                    capability=None,
                    params={"test_mode": "sleep", "sleep_s": 0.1},
                    timeout_s=10,
                )
            )
        )["task_id"]
        event_state = await client.wait_state(event_task, {"SUCCEEDED"}, timeout_s=15)
        event_connection = read_jsonl(self.interface_dir(event_id) / "connection.jsonl")
        event_timeout_rows = [
            row for row in event_connection if row.get("msg") == "event_timeout"
        ]
        event_handle.stop()

        task_id, task_handle = self.start_interface(
            "task-timeout",
            heartbeat_s=0.5,
            task_heartbeat_s=0.5,
            event_timeout_s=1,
        )
        await self.wait_registration(task_id)
        timed_task = (
            await client.submit(
                build_envelope(
                    instruction="task timeout layer",
                    interface_id=task_id,
                    capability=None,
                    params={"test_mode": "sleep", "sleep_s": 2, "_lease_seconds": 30},
                    timeout_s=0.8,
                )
            )
        )["task_id"]
        timed_state = await client.wait_state(timed_task, {"TIMED_OUT"}, timeout_s=10)
        timeout_rows = self.ledger_for("TASK_TIMEOUT", timed_task)
        lease_rows_for_task = self.ledger_for("LEASE_EXPIRED", timed_task)
        task_handle.stop()
        passed = (
            connection_error is not None
            and connection_elapsed < 2.5
            and bool(event_timeout_rows)
            and event_state["state"] == "SUCCEEDED"
            and len(self.ledger_for("TASK_TIMEOUT", event_task)) == 0
            and timed_state["state"] == "TIMED_OUT"
            and bool(timeout_rows)
            and len(lease_rows_for_task) == 0
        )
        return (
            passed,
            f"connection_timeout={type(connection_error).__name__} elapsed={connection_elapsed:.3f}s; event_timeout_logs={len(event_timeout_rows)}; task_timeout={timed_state['state']} independent_from_lease={len(lease_rows_for_task) == 0}",
            [
                str(self.interface_dir(event_id) / "connection.jsonl"),
                str(self.base / "data" / "ledger.jsonl"),
                str(self.transcript_path),
            ],
            {
                "connection_error_type": type(connection_error).__name__ if connection_error else None,
                "connection_elapsed_s": round(connection_elapsed, 3),
                "event_timeout_count": len(event_timeout_rows),
                "event_task_state": event_state["state"],
                "task_timeout_state": timed_state["state"],
            },
        )

    async def test_09_verdict_vocabulary(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        interface_id, handle = self.start_interface("verdict")
        await self.wait_registration(interface_id)
        words = ["通过", "带注通过", "条件通过", "未核", "驳回", "作废"]
        accepted: list[str] = []
        task_ids: list[str] = []
        for index, word in enumerate(words):
            response = await client.submit(
                build_envelope(
                    instruction=f"verdict vocabulary {word}",
                    interface_id=interface_id,
                    capability=None,
                    params={"test_mode": "echo_instruction"},
                    timeout_s=10,
                )
            )
            task_id = response["task_id"]
            task_ids.append(task_id)
            await client.wait_state(task_id, {"SUCCEEDED"}, timeout_s=10)
            request = {
                "task_id": task_id,
                "verdict": word,
                "checker": "runtime-self-test",
                "evidence_level": "E3",
                "deterministic_check": "result file and hash present",
                "independent_review": "自检，未独立验证",
                "physical_readback": "result file read back",
                "notes": "prototype vocabulary acceptance",
            }
            if word == "未核":
                request.update(
                    {
                        "owner": "runtime",
                        "min_action": f"open {self.base / 'evidence' / 'acceptance-results.json'} and complete independent review",
                        "due_at": "2026-11-06T03:00:00+08:00",
                    }
                )
            result = await client.verdict(request)
            if result.get("ok"):
                accepted.append(word)
        invalid_word = await client.verdict(
            {"task_id": task_ids[0], "verdict": "完成", "checker": "runtime-self-test"}
        )
        incomplete_unverified = await client.verdict(
            {"task_id": task_ids[1], "verdict": "未核", "checker": "runtime-self-test"}
        )
        ledger_verdicts = [
            row
            for row in self.ledger_for("VERDICT")
            if row.get("task_id") in task_ids
        ]
        observed = [row["verdict"] for row in ledger_verdicts]
        handle.stop()
        passed = (
            accepted == words
            and observed == words
            and invalid_word.get("reason") == "invalid_verdict_word"
            and incomplete_unverified.get("reason") == "unverified_exit_incomplete"
            and set(incomplete_unverified.get("missing", [])) == {"owner", "min_action", "due_at"}
        )
        return (
            passed,
            f"accepted words={observed}; invalid word rejected; incomplete 未核 missing={incomplete_unverified.get('missing')}",
            [str(self.base / "data" / "ledger.jsonl"), str(self.transcript_path)],
            {"accepted": accepted, "observed": observed, "invalid_word_response": invalid_word, "incomplete_unverified": incomplete_unverified},
        )

    async def test_10_real_runtime_chain(self) -> tuple[bool, str, list[str], dict[str, Any]]:
        client = await self.client()
        interface_id, handle = self.start_interface(
            "runtime-main",
            heartbeat_s=0.5,
            task_heartbeat_s=2,
            event_timeout_s=2,
            max_concurrency=1,
            executor="runtime",
        )
        await self.wait_registration(interface_id, timeout_s=20)
        instruction = (
            "Reply with exactly INTERFACE_V02_RUNTIME_OK. "
            "Do not run commands, use tools, or modify any files."
        )
        envelope = build_envelope(
            instruction=instruction,
            interface_id=interface_id,
            capability=None,
            params={},
            timeout_s=120,
        )
        submit = await client.submit(envelope)
        task_id = submit["task_id"]
        self.task_ids["real_runtime"] = task_id
        state = await client.wait_state(task_id, TERMINAL, timeout_s=180)
        await self.wait_until(
            lambda: self.pending_events(interface_id),
            lambda pending: len(pending) == 0,
            timeout_s=30,
            label="real runtime outbox drain",
        )
        pointer = state.get("result_pointer") or {}
        result_path = file_uri_to_path(pointer["uri"])
        stdout_text = result_path.read_text(encoding="utf-8", errors="replace")
        operations = read_jsonl(self.interface_dir(interface_id) / "operations.jsonl")
        runtime_ops = [
            row
            for row in operations
            if row.get("task_id") == task_id
            and row.get("action") == "invoke_runtime_exec"
        ]
        state_rows = self.ledger_for("TASK_STATE", task_id)
        states = [row["state"] for row in state_rows]
        events = self.ledger_for("TASK_EVENT", task_id)
        event_kinds = [row["kind"] for row in events]
        connection_rows = read_jsonl(self.interface_dir(interface_id) / "connection.jsonl")
        hub_connection_rows = read_jsonl(self.base / "logs" / "hub-connection.jsonl")
        result_hash_matches = sha256_file(result_path) == pointer.get("sha256")
        handle.stop()
        expected_states = ["QUEUED", "DISPATCHED", "CLAIMED", "RUNNING", "SUCCEEDED"]
        passed = (
            state["state"] == "SUCCEEDED"
            and all(item in states for item in expected_states)
            and all(item in event_kinds for item in ["accepted", "claimed", "progress", "result_ready", "completed"])
            and "INTERFACE_V02_RUNTIME_OK" in stdout_text
            and bool(runtime_ops)
            and result_hash_matches
            and bool(connection_rows)
            and bool(hub_connection_rows)
        )
        return (
            passed,
            f"real runtime chain terminal={state['state']}; state_path={states}; event_kinds={event_kinds}; result_sha_match={result_hash_matches}",
            [
                str(self.base / "data" / "ledger.jsonl"),
                str(self.interface_dir(interface_id) / "operations.jsonl"),
                str(self.interface_dir(interface_id) / "outbox.jsonl"),
                str(self.interface_dir(interface_id) / "connection.jsonl"),
                str(self.base / "logs" / "hub-connection.jsonl"),
                str(result_path),
            ],
            {
                "task_id": task_id,
                "interface_id": interface_id,
                "states": states,
                "event_kinds": event_kinds,
                "result_pointer": pointer,
                "runtime_operation_count": len(runtime_ops),
                "stdout_contains_expected": "INTERFACE_V02_RUNTIME_OK" in stdout_text,
            },
        )

    def nats_listener_pid(self) -> int:
        script = (
            "(Get-NetTCPConnection -State Listen -LocalPort 43321 -ErrorAction Stop)"
            ".OwningProcess"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        if completed.returncode != 0 or not completed.stdout.strip():
            raise RuntimeError(f"cannot resolve lab NATS PID: {completed.stderr[-300:]}")
        return int(completed.stdout.strip().splitlines()[-1])

    def verify_nats_pid(self, pid: int) -> None:
        script = (
            f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId = {pid}';"
            "if($p){$p.CommandLine}else{''}"
        )
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        command_line = completed.stdout.strip()
        expected_config = os.path.normcase(
            os.path.normpath(str((self.base / "nats-test.conf").resolve()))
        )
        actual_command = os.path.normcase(os.path.normpath(command_line))
        if expected_config not in actual_command or "nats-server" not in actual_command:
            raise RuntimeError("refusing to stop NATS process: not the independent lab instance")

    def stop_nats(self, pid: int) -> None:
        self.verify_nats_pid(pid)
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                f"Stop-Process -Id {pid} -Force -ErrorAction Stop",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(f"failed to stop lab NATS: {completed.stderr[-300:]}")

    def start_nats(self) -> ProcessHandle:
        out_path = self.log_dir / "nats-restart.stdout.log"
        err_path = self.log_dir / "nats-restart.stderr.log"
        nats_executable = os.environ.get("NATS_SERVER") or shutil.which("nats-server")
        if not nats_executable:
            raise RuntimeError("nats-server was not found; add it to PATH or set NATS_SERVER")
        command = [
            nats_executable,
            "-c",
            str(self.base / "nats-test.conf"),
        ]
        out_handle = out_path.open("wb")
        err_handle = err_path.open("wb")
        process = subprocess.Popen(
            command,
            cwd=str(self.base),
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=err_handle,
            creationflags=CREATE_NO_WINDOW,
        )
        out_handle.close()
        err_handle.close()
        handle = ProcessHandle(process, command, out_path, err_path)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            script = "try {(Invoke-RestMethod 'http://127.0.0.1:43322/varz' -TimeoutSec 1).server_id}catch{''}"
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=3,
                creationflags=CREATE_NO_WINDOW,
                check=False,
            )
            if completed.stdout.strip():
                return handle
            time.sleep(0.25)
        handle.stop()
        raise RuntimeError("lab NATS did not restart")

    def process_snapshot(self) -> dict[str, Any]:
        escaped_base = str(self.base).replace("'", "''")
        script = rf"""
$base = '{escaped_base}'
$config = Join-Path $base 'nats-test.conf'
Get-CimInstance Win32_Process | Where-Object {{
  $_.Name -eq 'nats-server.exe' -and $_.CommandLine -like "*$config*"
}} | Select-Object ProcessId,ParentProcessId,Name,CommandLine | ConvertTo-Json -Compress
"""
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        text = completed.stdout.strip()
        nats_process = json.loads(text) if text else None
        hub_candidates = []
        output = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-Command",
                (
                    f"$base = '{escaped_base}'; "
                    "Get-CimInstance Win32_Process | Where-Object {"
                    "$_.Name -eq 'python.exe' -and "
                    "$_.CommandLine -like \"*$base\\hub.py*\""
                    "} | Select-Object ProcessId,ParentProcessId,Name,CommandLine "
                    "| ConvertTo-Json -Compress"
                ),
            ],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=CREATE_NO_WINDOW,
            check=False,
        )
        if output.stdout.strip():
            parsed = json.loads(output.stdout.strip())
            hub_candidates = parsed if isinstance(parsed, list) else [parsed]
        return {
            "nats": nats_process if isinstance(nats_process, list) else [nats_process] if nats_process else [],
            "hub": hub_candidates,
            "ports": {"nats": 43321, "monitor": 43322},
        }

    async def run(self) -> int:
        self.log(
            f"RUN start run_id={self.run_id} python={sys.version.split()[0]} nats_url={self.nats_url}"
        )
        await self.cleanup_interfaces()
        command = f"{sys.executable} {Path(__file__).resolve()}"
        await self.run_case(1, "登记与 stale", command, self.test_01_stale)
        await self.run_case(2, "接入检查防冒名", command, self.test_02_impersonation)
        await self.run_case(3, "信封原样传达", command, self.test_03_exact_instruction)
        await self.run_case(5, "重复投递只执行一次", command, self.test_05_duplicate_delivery)
        await self.run_case(6, "租约回收与围栏", command, self.test_06_lease_fencing)
        await self.run_case(7, "资源串行与跨资源并发", command, self.test_07_resource_and_concurrency)
        await self.run_case(8, "超时分层", command, self.test_08_timeout_layers)
        await self.run_case(9, "裁定写入", command, self.test_09_verdict_vocabulary)
        await self.run_case(10, "真 runtime 全链与账目互证", command, self.test_10_real_runtime_chain)
        await self.run_case(4, "断网 5 分钟补投", command, self.test_04_outage_five_minutes)
        await self.cleanup_interfaces()
        process_info = self.process_snapshot()
        atomic_write_json(self.base / "runtime" / "process-info.json", process_info)
        ordered = {str(index): self.results.get(str(index), {"verdict": "不通过", "summary": "缺失"}) for index in range(1, 11)}
        all_pass = all(value.get("verdict") == "通过" for value in ordered.values())
        final = {
            "run_id": self.run_id,
            "self_test": True,
            "independent_verification": "not performed",
            "overall": "通过" if all_pass else "不通过",
            "evidence_level": "E3-自证，未独立验证",
            "command": command,
            "exit_code": 0 if all_pass else 1,
            "results": ordered,
            "process_info": process_info,
            "finished_at": now_iso(),
        }
        atomic_write_json(self.results_path, final)
        atomic_write_json(self.base / "runtime" / "acceptance-summary.json", final)
        self.log(
            f"RUN end overall={final['overall']} exit_code={final['exit_code']} results={self.results_path}"
        )
        return 0 if all_pass else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base",
        type=Path,
        default=Path(__file__).resolve().parent,
    )
    parser.add_argument(
        "--nats-url",
        default="nats://127.0.0.1:43321",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    runner = AcceptanceRunner(args.base.resolve(), args.nats_url)
    try:
        return asyncio.run(runner.run())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
