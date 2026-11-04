#!/usr/bin/env python3
"""Recompute deterministic checks over the completed tianshu v0.2 lab run."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from core import atomic_write_json, now_iso


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_file_uri(uri: str) -> Path:
    prefix = "file:///"
    if not uri.startswith(prefix):
        raise ValueError(f"unsupported result URI: {uri}")
    return Path(uri[len("file:///") :])


def run(base: Path) -> int:
    acceptance_path = base / "evidence" / "acceptance-results.json"
    edge_path = base / "evidence" / "contract-edge-results.json"
    shutdown_path = base / "evidence" / "shutdown.json"
    acceptance = json.loads(acceptance_path.read_text(encoding="utf-8"))
    edge = json.loads(edge_path.read_text(encoding="utf-8"))
    shutdown = json.loads(shutdown_path.read_text(encoding="utf-8-sig"))
    run_id = acceptance["run_id"]
    transcript = (
        base
        / "logs"
        / "acceptance"
        / run_id
        / "acceptance-transcript.log"
    )
    transcript_text = transcript.read_text(encoding="utf-8")
    results = acceptance["results"]
    item10 = results["10"]
    item11 = results["11"]
    result_pointer = item10["output"]["result_pointer"]
    result_path = parse_file_uri(result_pointer["uri"])
    action_path = Path(item11["output"]["approved_action_path"])
    approval_key = base / "data" / "approval-signing.key"
    approval_token = (
        base
        / ".lab-tokens"
        / (
            f"{item11['output']['direct_bypass']['interface_id']}.token"
            if "interface_id" in item11["output"]["direct_bypass"]
            else ""
        )
    )

    # The acceptance result lists evidence paths; recover the lab token path
    # without reading its value.
    approval_token = next(
        (
            Path(value)
            for value in item11["evidence_paths"]
            if value.endswith(".token")
        ),
        approval_token,
    )

    ledger_rows = [
        json.loads(line)
        for line in (base / "data" / "ledger.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    approved_task = item11["output"]["approved_task_id"]
    missing_task = item11["output"]["missing_task_id"]
    reused_task = item11["output"]["reused_task_id"]
    approval_verified = [
        row
        for row in ledger_rows
        if row.get("record") == "APPROVAL_VERIFIED"
        and row.get("task_id") == approved_task
    ]
    approval_rejected = [
        row
        for row in ledger_rows
        if row.get("record") == "APPROVAL_REJECTED"
        and row.get("task_id") in {missing_task, reused_task}
    ]

    offline_dir = (
        base
        / "runtime"
        / "interfaces"
        / f"accept-{run_id}-offline"
    )
    outbox = [
        json.loads(line)
        for line in (offline_dir / "outbox.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    ack_rows = [
        json.loads(line)
        for line in (offline_dir / "outbox-acks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip()
    ]
    offline_task = results["4"]["output"]["task_id"]
    offline_events = [
        row
        for row in outbox
        if row.get("record") == "EVENT" and row.get("task_id") == offline_task
    ]
    success_acks = [
        row
        for row in ack_rows
        if row.get("record") == "ACK"
        and row.get("ok") is True
        and str(row.get("event_id", "")).startswith(offline_task + ":")
    ]
    pending_ids = {
        f"{row['task_id']}:{row['seq']}" for row in offline_events
    } - {
        row["event_id"]
        for row in ack_rows
        if row.get("record") == "ACK" and row.get("ok") is True
    }

    checks: dict[str, dict[str, Any]] = {
        "acceptance_overall": {
            "ok": acceptance.get("overall") == "通过"
            and acceptance.get("exit_code") == 0,
            "observed": {
                "overall": acceptance.get("overall"),
                "exit_code": acceptance.get("exit_code"),
            },
        },
        "all_eleven_items_pass": {
            "ok": len(results) == 11
            and all(results[str(i)].get("verdict") == "通过" for i in range(1, 12)),
            "item_count": len(results),
            "verdicts": {
                key: results[key].get("verdict")
                for key in sorted(results, key=int)
            },
        },
        "transcript_has_no_harness_error": {
            "ok": transcript_text.count("CHECK ")
            - transcript_text.count("verdict=不通过")
            == 11
            and "\nERROR item=" not in transcript_text
            and "overall=通过 exit_code=0" in transcript_text,
            "pass_checks": transcript_text.count("verdict=通过"),
            "fail_checks": transcript_text.count("verdict=不通过"),
            "harness_errors": transcript_text.count("\nERROR item="),
        },
        "outage_exceeds_300_seconds": {
            "ok": results["4"]["output"]["outage_seconds"] >= 300,
            "outage_seconds": results["4"]["output"]["outage_seconds"],
            "pending_during_outage": results["4"]["output"][
                "pending_during_outage"
            ],
        },
        "outage_sequences_match": {
            "ok": results["4"]["output"]["local_seqs"]
            == results["4"]["output"]["central_seqs"]
            and len(offline_events) == len(success_acks)
            and not pending_ids,
            "local_seqs": results["4"]["output"]["local_seqs"],
            "central_seqs": results["4"]["output"]["central_seqs"],
            "success_ack_count": len(success_acks),
            "pending_ids": sorted(pending_ids),
        },
        "tianshu_readonly_result": {
            "ok": result_path.is_file()
            and sha256(result_path) == result_pointer.get("sha256")
            and item10["output"].get("source_unchanged") is True
            and item10["output"].get("deterministic_result") == "通过",
            "result_path": str(result_path),
            "result_sha256": sha256(result_path) if result_path.is_file() else None,
            "pointer_sha256": result_pointer.get("sha256"),
            "source_unchanged": item10["output"].get("source_unchanged"),
        },
        "approval_enforced_and_not_reused": {
            "ok": item11["output"].get("approved_external_operation_count") == 1
            and item11["output"]["reused_rejection"].get("reason")
            == "approval_task_mismatch"
            and item11["output"]["direct_bypass"].get("ok") is False
            and "approval_task_mismatch"
            in item11["output"]["direct_bypass"].get("reason", "")
            and len(approval_verified) >= 1
            and {
                row.get("reason") for row in approval_rejected
            }
            >= {"approval_missing", "approval_task_mismatch"}
            and action_path.is_file(),
            "approval_verified_records": len(approval_verified),
            "approval_rejected_records": len(approval_rejected),
            "rejected_reasons": sorted(
                {
                    str(row.get("reason"))
                    for row in approval_rejected
                    if row.get("reason")
                }
            ),
            "external_action_path": str(action_path),
        },
        "separate_lab_credentials_exist": {
            "ok": approval_key.is_file()
            and approval_key.stat().st_size > 0
            and approval_token.is_file()
            and approval_token.stat().st_size > 0,
            "approval_signing_key_present": approval_key.is_file(),
            "interface_token_present": approval_token.is_file(),
            "credential_values_recorded": False,
        },
        "contract_edge_tests": {
            "ok": edge.get("overall") == "通过"
            and edge.get("exit_code") == 0
            and all(
                check.get("ok") is True
                for check in edge.get("checks", {}).values()
            ),
            "overall": edge.get("overall"),
            "exit_code": edge.get("exit_code"),
            "checks": {
                key: value.get("ok")
                for key, value in edge.get("checks", {}).items()
            },
        },
        "lab_processes_stopped": {
            "ok": not shutdown.get("after")
            and not any(
                "LISTENING" in line
                for line in shutdown.get("lab_listener_lines", [])
            ),
            "after_process_count": len(shutdown.get("after", [])),
            "listener_lines": shutdown.get("lab_listener_lines", []),
        },
    }

    source_files = [
        "core.py",
        "hub.py",
        "interface.py",
        "lab_client.py",
        "provision_tokens.py",
        "fake_responder.py",
        "test_acceptance.py",
        "test_contract_edges.py",
        "verify_lab.py",
        "nats-test.conf",
        "start_lab.ps1",
        "stop_lab.ps1",
    ]
    source_inventory = {}
    for name in source_files:
        path = base / name
        source_inventory[name] = {
            "bytes": path.stat().st_size,
            "sha256": sha256(path),
        }

    overall = all(check["ok"] for check in checks.values())
    result = {
        "run_id": run_id,
        "contract_version": "0.2",
        "role": "tianshu-lab",
        "overall": "通过" if overall else "不通过",
        "exit_code": 0 if overall else 1,
        "evidence_level": "E1-可复现自测；E3-未独立验证",
        "independent_verification": "not performed",
        "checks": checks,
        "artifacts": {
            "acceptance_results": {
                "path": str(acceptance_path),
                "bytes": acceptance_path.stat().st_size,
                "sha256": sha256(acceptance_path),
            },
            "acceptance_transcript": {
                "path": str(transcript),
                "bytes": transcript.stat().st_size,
                "sha256": sha256(transcript),
            },
            "contract_edge_results": {
                "path": str(edge_path),
                "bytes": edge_path.stat().st_size,
                "sha256": sha256(edge_path),
            },
            "ledger": {
                "path": str(base / "data" / "ledger.jsonl"),
                "bytes": (base / "data" / "ledger.jsonl").stat().st_size,
                "sha256": sha256(base / "data" / "ledger.jsonl"),
            },
            "tianshu_result": {
                "path": str(result_path),
                "bytes": result_path.stat().st_size,
                "sha256": sha256(result_path),
            },
            "approval_action": {
                "path": str(action_path),
                "bytes": action_path.stat().st_size,
                "sha256": sha256(action_path),
            },
            "shutdown_evidence": {
                "path": str(shutdown_path),
                "bytes": shutdown_path.stat().st_size,
                "sha256": sha256(shutdown_path),
            },
        },
        "source_inventory": source_inventory,
        "checked_at": now_iso(),
    }
    output = base / "evidence" / "final-verification.json"
    atomic_write_json(output, result)
    print(
        json.dumps(
            {
                "overall": result["overall"],
                "exit_code": result["exit_code"],
                "run_id": run_id,
                "checks": {
                    name: value["ok"] for name, value in checks.items()
                },
                "final_verification": str(output),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    return result["exit_code"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parent)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    return run(args.base.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
