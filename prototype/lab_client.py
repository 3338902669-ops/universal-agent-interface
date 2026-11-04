#!/usr/bin/env python3
"""Small controller client used by tests and manual lab probes."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import nats

from core import CONTRACT_VERSION, canonical_json, envelope_digest, now_iso


def build_envelope(
    *,
    instruction: str,
    interface_id: str | None = None,
    capability: str | None = "codex_exec",
    resource_key: str | None = None,
    params: dict[str, Any] | None = None,
    timeout_s: float = 120,
    cls: str = "readonly",
    created_by: str = "acceptance-driver",
    attachments: list[dict[str, str]] | None = None,
    skill_ref: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if bool(interface_id) == bool(capability):
        raise ValueError("exactly one of interface_id and capability is required")
    target = (
        {"interface_id": interface_id}
        if interface_id
        else {"capability": capability}
    )
    return {
        "contract_version": CONTRACT_VERSION,
        "task_id": str(uuid.uuid4()),
        "idempotency_key": f"lab-{uuid.uuid4()}",
        "target": target,
        "instruction": instruction,
        "skill_ref": skill_ref,
        "resource_key": resource_key,
        "params": params or {},
        "attachments": attachments or [],
        "timeout_s": timeout_s,
        "cls": cls,
        "created_by": created_by,
        "created_at": now_iso(),
        "trace_id": str(uuid.uuid4()),
    }


class LabClient:
    def __init__(self, nats_url: str):
        self.nats_url = nats_url
        self.nc = None

    async def connect(self) -> None:
        self.nc = await nats.connect(
            self.nats_url,
            name="interface-prototype-acceptance-driver",
            connect_timeout=2,
            max_reconnect_attempts=-1,
            reconnect_time_wait=1,
        )

    async def request(
        self, subject: str, value: dict[str, Any], timeout: float = 5
    ) -> dict[str, Any]:
        if not self.nc:
            raise RuntimeError("client is not connected")
        reply = await self.nc.request(
            subject, canonical_json(value), timeout=timeout
        )
        return json.loads(reply.data.decode("utf-8"))

    async def submit(self, envelope: dict[str, Any]) -> dict[str, Any]:
        return await self.request("hub.submit", envelope, timeout=5)

    async def state(self, task_id: str) -> dict[str, Any]:
        return await self.request("hub.state", {"task_id": task_id}, timeout=5)

    async def verdict(self, value: dict[str, Any]) -> dict[str, Any]:
        return await self.request("hub.verdict", value, timeout=5)

    async def debug(self, value: dict[str, Any]) -> dict[str, Any]:
        return await self.request("hub.debug", value, timeout=5)

    async def reconcile(self) -> dict[str, Any]:
        return await self.request("hub.reconcile", {}, timeout=10)

    async def wait_state(
        self,
        task_id: str,
        states: set[str],
        *,
        timeout_s: float = 30,
        interval_s: float = 0.15,
    ) -> dict[str, Any]:
        deadline = asyncio.get_running_loop().time() + timeout_s
        latest: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            latest = await self.state(task_id)
            if latest.get("state") in states:
                return latest
            await asyncio.sleep(interval_s)
        raise TimeoutError(
            f"task {task_id} did not reach {sorted(states)}; latest={latest}"
        )

    async def close(self) -> None:
        if self.nc:
            await self.nc.drain()


async def _main(args: argparse.Namespace) -> int:
    client = LabClient(args.nats_url)
    await client.connect()
    try:
        if args.command == "submit":
            envelope = json.loads(Path(args.file).read_text(encoding="utf-8"))
            response = await client.submit(envelope)
        elif args.command == "state":
            response = await client.state(args.task_id)
        elif args.command == "debug":
            response = await client.debug(json.loads(Path(args.file).read_text(encoding="utf-8")))
        else:
            response = await client.reconcile()
        print(json.dumps(response, ensure_ascii=False, indent=2))
    finally:
        await client.close()
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--nats-url", default="nats://127.0.0.1:43321")
    sub = parser.add_subparsers(dest="command", required=True)
    submit = sub.add_parser("submit")
    submit.add_argument("--file", required=True)
    state = sub.add_parser("state")
    state.add_argument("--task-id", required=True)
    debug = sub.add_parser("debug")
    debug.add_argument("--file", required=True)
    sub.add_parser("reconcile")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main(parse_args())))
