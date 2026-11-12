#!/usr/bin/env python3
"""Run the stage2 self-test battery and write a verifiable raw record (E-04).

This script records what *this* process observed. Independent re-run still
requires a seat that did not implement the change; the JSON is only a
checkable raw record (evidence_level E3).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
EVIDENCE_DIR = os.path.join(ROOT, "evidence")

SUITES = [
    "test_projection.py",
    "test_io.py",
    "test_unified.py",
    "test_protocol_specs.py",
    "test_bridge_joins.py",
    "test_a2a_server.py",
    "test_a03_stream.py",
    "test_identity.py",
    "test_p1_batch.py",
    "test_a09_singleton.py",
    "test_b02_ao_refs.py",
    "test_p2_batch.py",
]

RESULT_RE = re.compile(r"^RESULT:\s*(.+)$", re.M)


def git_sha() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True, stderr=subprocess.DEVNULL
        )
        return out.strip()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return "unknown"


def run_suite(name: str) -> dict:
    started = time.time()
    proc = subprocess.run(
        [sys.executable, name],
        cwd=HERE,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    elapsed = round(time.time() - started, 4)
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""
    combined = stdout + ("\n" + stderr if stderr else "")
    found = RESULT_RE.findall(combined)
    result_line = found[-1] if found else None
    return {
        "suite": name,
        "exit_code": proc.returncode,
        "elapsed_s": elapsed,
        "result_line": ("RESULT: " + result_line) if result_line else None,
        "stdout_tail": stdout[-2000:],
        "stderr_tail": stderr[-2000:],
    }


def main() -> int:
    sha = git_sha()
    records = []
    overall = 0
    for name in SUITES:
        rec = run_suite(name)
        records.append(rec)
        print("%s  exit=%s  elapsed_s=%s  %s" % (
            name, rec["exit_code"], rec["elapsed_s"], rec["result_line"] or "(no RESULT line)",
        ))
        if rec["exit_code"] != 0:
            overall = 1

    payload = {
        "evidence_level": "E3",
        "note": "独立复跑需由未参与实现的席位执行，本文件只是可核证的原始记录。",
        "git_sha": sha,
        "python": sys.version.replace("\n", " "),
        "executable": sys.executable,
        "recorded_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "suites": records,
        "overall_exit_code": overall,
        "passed": overall == 0,
        "suite_count": len(records),
        "failed_suites": [r["suite"] for r in records if r["exit_code"] != 0],
    }

    os.makedirs(EVIDENCE_DIR, exist_ok=True)
    out_path = os.path.join(EVIDENCE_DIR, "independent-%s.json" % sha)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print("RESULT: %d/%d suites exit 0  evidence=%s" % (
        sum(1 for r in records if r["exit_code"] == 0), len(records), out_path,
    ))
    return overall


if __name__ == "__main__":
    sys.exit(main())
