#!/usr/bin/env python3
"""Deliberately unauthenticated echo responder used by acceptance test #2."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import nats

from core import append_log, canonical_json, now_iso


async def run(interface_id: str, nats_url: str, log_path: Path) -> None:
    nc = await nats.connect(
        nats_url,
        name=f"fake-responder-{interface_id}",
        max_reconnect_attempts=-1,
    )

    async def handler(msg):
        try:
            request = json.loads(msg.data.decode("utf-8"))
        except Exception:
            return
        if request.get("kind") != "echo":
            return
        append_log(
            log_path,
            "fake-responder",
            "forged_echo_sent",
            interface_id=interface_id,
            challenge_id=request.get("challenge_id"),
        )
        response = {
            "kind": "echo_ack",
            "interface_id": interface_id,
            "challenge_id": request.get("challenge_id"),
            "marker": request.get("marker"),
            "proof": "forged-without-token",
            "forged_at": now_iso(),
        }
        if msg.reply:
            await msg.respond(canonical_json(response))

    await nc.subscribe(f"iface.{interface_id}.control", cb=handler)
    print(
        json.dumps(
            {
                "event": "fake_responder_ready",
                "interface_id": interface_id,
                "forges_echo_only": True,
            }
        ),
        flush=True,
    )
    await asyncio.Event().wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--interface-id", required=True)
    parser.add_argument("--nats-url", required=True)
    parser.add_argument("--log", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(run(args.interface_id, args.nats_url, args.log.resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
