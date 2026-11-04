#!/usr/bin/env python3
"""Focused contract-boundary checks used after the ten-item acceptance run."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from core import (
    atomic_write_json,
    canonical_json,
    make_proof,
    now_iso,
    path_to_file_uri,
    read_jsonl,
    sha256_file,
)
from lab_client import build_envelope
from test_acceptance import AcceptanceRunner


async def run_checks(base: Path, nats_url: str) -> int:
    runner = AcceptanceRunner(base, nats_url)
    checks: dict[str, dict[str, Any]] = {}
    try:
        await runner.cleanup_interfaces()
        client = await runner.client()
        interface_id, _handle = runner.start_interface(
            "contract-edges",
            heartbeat_s=0.5,
            task_heartbeat_s=2,
            event_timeout_s=1,
            max_concurrency=1,
            executor="test",
        )
        await runner.wait_registration(interface_id)

        oversized = build_envelope(
            instruction="x" * (256 * 1024 + 1),
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "echo_instruction"},
            timeout_s=10,
        )
        oversized_response = await client.submit(oversized)
        checks["envelope_256kb_limit"] = {
            "ok": oversized_response.get("reason") == "envelope_too_large",
            "reason": oversized_response.get("reason"),
        }

        missing_timeout = build_envelope(
            instruction="missing required field",
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "echo_instruction"},
            timeout_s=10,
        )
        missing_timeout.pop("timeout_s")
        missing_response = await client.submit(missing_timeout)
        checks["required_fields"] = {
            "ok": str(missing_response.get("reason", "")).startswith(
                "missing_fields:"
            )
            and "timeout_s" in str(missing_response.get("reason")),
            "reason": missing_response.get("reason"),
        }

        fixture = base / "fixtures" / f"hash-mismatch-{runner.run_id}.json"
        atomic_write_json(fixture, {"fixture": "attachment-hash-mismatch"})
        hash_mismatch = build_envelope(
            instruction="attachment hash mismatch",
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "echo_instruction"},
            timeout_s=10,
            attachments=[
                {
                    "uri": path_to_file_uri(fixture),
                    "sha256": "0" * 64,
                }
            ],
        )
        mismatch_response = await client.submit(hash_mismatch)
        checks["attachment_hash"] = {
            "ok": mismatch_response.get("reason")
            == "attachment_hash_mismatch",
            "reason": mismatch_response.get("reason"),
            "fixture_sha256": sha256_file(fixture),
        }

        invalid_target = build_envelope(
            instruction="target must name exactly one route",
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "echo_instruction"},
            timeout_s=10,
        )
        invalid_target["target"] = {
            "interface_id": interface_id,
            "capability": "runtime_exec",
        }
        target_response = await client.submit(invalid_target)
        checks["target_exactly_one"] = {
            "ok": target_response.get("reason")
            == "target_must_name_exactly_one",
            "reason": target_response.get("reason"),
        }

        duplicate = build_envelope(
            instruction="idempotency and event deduplication",
            interface_id=interface_id,
            capability=None,
            params={"test_mode": "echo_instruction"},
            timeout_s=10,
        )
        first_submit = await client.submit(duplicate)
        second_submit = await client.submit(duplicate)
        state = await client.wait_state(
            first_submit["task_id"], {"SUCCEEDED"}, timeout_s=15
        )
        runner.task_ids["contract_edges"] = first_submit["task_id"]

        local_events = [
            row
            for row in read_jsonl(
                runner.interface_dir(interface_id) / "outbox.jsonl"
            )
            if row.get("record") == "EVENT"
            and row.get("task_id") == first_submit["task_id"]
        ]
        progress_event = next(
            row for row in local_events if row.get("kind") == "progress"
        )
        event_value = {
            key: progress_event[key]
            for key in (
                "task_id",
                "trace_id",
                "seq",
                "fencing_generation",
                "kind",
                "payload",
                "created_at",
            )
        }
        token_path = base / ".lab-tokens" / f"{interface_id}.token"
        token_value = token_path.read_text(encoding="utf-8").strip()
        event_request = make_proof(
            token_value,
            {
                "kind": "event",
                "interface_id": interface_id,
                "event": event_value,
                "sent_at": now_iso(),
            },
        )
        event_reply = await client.nc.request(
            "hub.event", canonical_json(event_request), timeout=5
        )
        event_result = json.loads(event_reply.data.decode("utf-8"))

        task_events = runner.ledger_for(
            "TASK_EVENT", first_submit["task_id"]
        )
        duplicate_events = runner.ledger_for(
            "EVENT_DUPLICATE", first_submit["task_id"]
        )
        inbox = read_jsonl(runner.interface_dir(interface_id) / "inbox.jsonl")
        execution_rows = [
            row
            for row in inbox
            if row.get("task_id") == first_submit["task_id"]
            and row.get("record") == "EXECUTION_STARTED"
        ]
        unique_central_seqs = sorted(
            int(row["seq"]) for row in task_events
        )
        local_seqs = sorted(int(row["seq"]) for row in local_events)
        checks["submit_idempotency_key"] = {
            "ok": bool(second_submit.get("duplicate"))
            and second_submit.get("task_id") == first_submit.get("task_id")
            and len(execution_rows) == 1,
            "first_task_id": first_submit.get("task_id"),
            "second_task_id": second_submit.get("task_id"),
            "duplicate": second_submit.get("duplicate"),
            "execution_count": len(execution_rows),
        }
        checks["event_task_seq_dedup"] = {
            "ok": bool(event_result.get("duplicate"))
            and len(duplicate_events) == 1
            and unique_central_seqs == local_seqs
            and len(unique_central_seqs) == len(set(unique_central_seqs)),
            "duplicate_response": event_result,
            "duplicate_record_count": len(duplicate_events),
            "central_seqs": unique_central_seqs,
            "local_seqs": local_seqs,
            "terminal_state": state.get("state"),
        }

        all_ok = all(item["ok"] for item in checks.values())
        result = {
            "run_id": runner.run_id,
            "self_test": True,
            "evidence_level": "E3-自证，未独立验证",
            "overall": "通过" if all_ok else "不通过",
            "exit_code": 0 if all_ok else 1,
            "checks": checks,
            "evidence_paths": [
                str(base / "data" / "ledger.jsonl"),
                str(runner.interface_dir(interface_id) / "inbox.jsonl"),
                str(runner.interface_dir(interface_id) / "outbox.jsonl"),
                str(token_path),
                str(fixture),
            ],
            "approval_or_token_value_recorded": False,
            "checked_at": now_iso(),
        }
        atomic_write_json(base / "evidence" / "contract-edge-results.json", result)
        print(
            json.dumps(
                {
                    "overall": result["overall"],
                    "exit_code": result["exit_code"],
                    "run_id": runner.run_id,
                    "checks": {
                        name: {
                            "ok": value["ok"],
                            "reason": value.get("reason"),
                        }
                        for name, value in checks.items()
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result["exit_code"]
    finally:
        await runner.cleanup_interfaces()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--nats-url", default="nats://127.0.0.1:43331")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(run_checks(args.base.resolve(), args.nats_url))


if __name__ == "__main__":
    raise SystemExit(main())
