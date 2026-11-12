"""E-04 独立复跑：在 CI 里把每个套件跑一遍，输出可逐字比对的结果清单。

设计要点（为什么不是"再跑一次 CI"）：
  * 结果清单只保留**语义**：每条的 PASS/FAIL 名称 + 退出码 + 通过/失败计数，
    去掉耗时、临时目录、绝对路径等易噪内容，使两台不同 runner 的清单应当逐字相同。
  * 比对由 `compare_reruns.py` 执行，两个 job 各产一份清单，第三个 job 比对。
    两份清单不一致即判 FAIL —— 复跑不是"再绿一次"，而是"两次独立执行结果必须等价"。

用法：
    python bench/ci_suites.py --out rerun-A.json
    python bench/ci_suites.py --out rerun-B.json
    python bench/compare_reruns.py rerun-A.json rerun-B.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (工作目录相对 ROOT, 命令, 名称)
SUITES: list[tuple[str, list[str], str]] = [
    ("protocol/tests/conformance", ["run_conformance.py"], "protocol-conformance"),
    ("stage2", ["test_projection.py"], "stage2-test_projection"),
    ("stage2", ["test_a2a_server.py"], "stage2-test_a2a_server"),
    ("stage2", ["test_io.py"], "stage2-test_io"),
    ("stage2", ["test_bridge_joins.py"], "stage2-test_bridge_joins"),
    ("stage2", ["test_unified.py"], "stage2-test_unified"),
    ("stage2", ["test_protocol_specs.py"], "stage2-test_protocol_specs"),
    ("stage2", ["test_a03_stream.py"], "stage2-test_a03_stream"),
    ("stage2", ["test_identity.py"], "stage2-test_identity"),
    ("stage2", ["test_p1_batch.py"], "stage2-test_p1_batch"),
    ("stage2", ["test_a09_singleton.py"], "stage2-test_a09_singleton"),
    ("stage2", ["test_b02_ao_refs.py"], "stage2-test_b02_ao_refs"),
    ("stage2", ["test_p2_batch.py"], "stage2-test_p2_batch"),
    ("stage2", ["test_a02_inbound.py"], "stage2-test_a02_inbound"),
    ("stage2", ["test_a05_second_runtime.py"], "stage2-test_a05_second_runtime"),
    ("stage2", ["check-cross-repo-vocab.py"], "cross-repo-vocab"),
    ("bench", ["run_benchmark.py", "--self-check"], "bench-baseline"),
    ("bench", ["test_rerun_compare.py"], "bench-rerun-compare"),
    ("bench", ["test_lab_gate.py"], "bench-lab-gate"),
    ("bench/controlled_experiment", ["test_controlled.py"], "bench-controlled-experiment"),
]

# 需要真实 lab（nats broker + hub 进程）的套件。
# 只要 NATS_URL 指向一个活着的 lab，就必须**真跑**——CI 里由 bench/lab.py 先把 lab 拉起来。
# 确实没有 lab 时才记 skipped 并写明原因，绝不伪装成 PASS。
REQUIRES_LAB: dict[str, tuple[str, str]] = {
    "stage2-test_a02_inbound": ("NATS_URL", "requires_lab_nats_and_hub_set_NATS_URL"),
}


def _has_runtime_binary() -> bool:
    """A-05 要在真 runtime 上跑（codex 原生 / dsh 适配）；干净 runner 上没有装。"""
    if os.environ.get("UAI_RUNTIME_BIN"):
        return True
    return any(shutil.which(x) for x in ("dsh", "codex", "codex.exe", "dsh.exe"))


# 前置条件不是环境变量而是「机器上得真有这个东西」。
# 注意 A-05 不在此表里：它的解析层与失败层不依赖任何二进制，必须真跑；
# 只有「真实调用」那一层在缺 runtime 时由测试自己记 SKIP（见该文件的 §3）。
PREREQ: dict[str, tuple[object, str]] = {}

# 只保留语义行：PASS/FAIL 名称与计数
SIGNAL_RE = re.compile(r"^(PASS|FAIL)\b|^RESULT:|^[A-Za-z0-9_.-]+:\s*(PASS|FAIL)\b|^ok\b|^not ok\b|^[0-9]+/[0-9]+")


def run_suite(workdir: str, argv: list[str], name: str) -> dict:
    cwd = ROOT / workdir
    if not (cwd / argv[0]).exists():
        return {"name": name, "skipped": True, "reason": "missing " + argv[0]}
    lab = REQUIRES_LAB.get(name)
    if lab and not os.environ.get(lab[0]):
        return {"name": name, "skipped": True, "reason": lab[1]}
    pre = PREREQ.get(name)
    if pre and not pre[0]():
        return {"name": name, "skipped": True, "reason": pre[1]}
    try:
        proc = subprocess.run(
            [sys.executable, *argv],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
        )
        code, out = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        return {"name": name, "exit": "timeout", "signal": []}

    signal = [ln.strip() for ln in out.splitlines() if SIGNAL_RE.match(ln.strip()) and ln.strip()]
    return {
        "name": name,
        "exit": code,
        "signal": signal,
        "digest": hashlib.sha256("\n".join(signal).encode("utf-8")).hexdigest(),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", nargs="*", help="只跑指定套件名")
    args = ap.parse_args(argv)

    rows = []
    for workdir, cmd, name in SUITES:
        if args.only and name not in args.only:
            continue
        row = run_suite(workdir, cmd, name)
        rows.append(row)
        if row.get("skipped"):
            print("%-38s SKIP  %s" % (name, row.get("reason")))
        else:
            print("%-38s exit=%-4s lines=%s" % (name, row.get("exit"), len(row.get("signal") or [])))

    payload = {
        "schema": "uai.bench.rerun/1",
        "python": platform.python_version(),
        "host": platform.system(),
        "suites": rows,
        "failed": [r["name"] for r in rows if not r.get("skipped") and r.get("exit") != 0],
        "skipped": [r["name"] for r in rows if r.get("skipped")],
    }
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("FAILED=%d/%d SKIPPED=%d -> %s" % (len(payload["failed"]), len(rows), len(payload["skipped"]), args.out))
    return 0 if not payload["failed"] else 1


if __name__ == "__main__":
    sys.exit(main())
