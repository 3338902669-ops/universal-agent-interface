"""受控对照实验：判分器 + 运行器（E-02）。

判分器必须是纯函数：同输入必得同输出，不读网络/时钟/随机。
运行器把每次尝试落成一条 JSONL，字段见 DESIGN.md §2/§4。
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures" / "tasks.jsonl"


# ---------------- 判分器（纯函数） ----------------
def score(task: dict, output: str) -> tuple[bool, str]:
    """返回 (是否通过, 说明)。纯函数：只看 task 与 output。"""
    spec = task.get("scoring") or {}
    kind = spec.get("kind")
    text = (output or "").strip()

    if kind == "exact":
        expect = str(spec.get("expect", ""))
        ok = text == expect
        return ok, "exact:%r" % expect

    if kind == "regex":
        pattern = spec.get("pattern", "")
        ok = re.fullmatch(pattern, text) is not None
        return ok, "regex:%s" % pattern

    if kind == "numeric":
        try:
            got = float(text)
            want = float(spec.get("expect"))
            tol = float(spec.get("tolerance", 0))
        except Exception:
            return False, "numeric:unparseable"
        ok = abs(got - want) <= tol
        return ok, "numeric:%s±%s" % (want, tol)

    return False, "unknown_scoring_kind:%r" % (kind,)


def load_tasks(path: Path = FIXTURES) -> list[dict]:
    tasks = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                tasks.append(json.loads(line))
    return tasks


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------- 运行器 ----------------
def run_one(task: dict, cmd_tpl: str, seat: str, arm: str) -> dict:
    """跑一道题。分类：pass / capability_failure / channel_failure。"""
    timeout = int(task.get("timeout_s", 1200))
    cmd = cmd_tpl.replace("{prompt}", task["prompt"]).replace("{timeout}", str(timeout))
    started = time.time()
    rec = {
        "task_id": task["id"], "seat": seat, "arm": arm,
        "started_at": started, "timeout_s": timeout,
    }
    try:
        completed = subprocess.run(
            shlex.split(cmd) if os.name != "nt" else cmd,
            shell=(os.name == "nt"),
            capture_output=True, text=True, timeout=timeout,
        )
        rec["exit_code"] = completed.returncode
        rec["stdout"] = completed.stdout[-4000:]
        rec["stderr"] = completed.stderr[-2000:]
        rec["duration_s"] = round(time.time() - started, 3)

        if completed.returncode != 0 and not completed.stdout.strip():
            rec["outcome"] = "channel_failure"
            rec["reason"] = "nonzero_exit_no_output"
            return rec

        ok, why = score(task, completed.stdout)
        rec["outcome"] = "pass" if ok else "capability_failure"
        rec["reason"] = why
        return rec

    except subprocess.TimeoutExpired:
        rec["outcome"] = "channel_failure"
        rec["reason"] = "timeout"
        rec["duration_s"] = round(time.time() - started, 3)
        return rec
    except FileNotFoundError as e:
        rec["outcome"] = "channel_failure"
        rec["reason"] = "command_not_found:%s" % e
        return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seat", required=True)
    ap.add_argument("--arm", required=True, choices=["with_skill", "without_skill"])
    ap.add_argument("--cmd", required=True, help="命令模板，用 {prompt} 与 {timeout} 占位")
    ap.add_argument("--out", required=True)
    ap.add_argument("--tasks", default=str(FIXTURES))
    args = ap.parse_args(argv)

    tasks_path = Path(args.tasks)
    tasks = load_tasks(tasks_path)
    if not tasks:
        print("no_tasks", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    raw = out / "attempts.jsonl"
    meta = out / "meta.json"

    meta_rec = {
        "seat": args.seat, "arm": args.arm, "cmd_template": args.cmd,
        "fixture_sha256": file_sha256(tasks_path),
        "scoring_frozen_sha256": file_sha256(Path(__file__).resolve()),
        "python": sys.version.split()[0], "started_at": time.time(),
        "tasks_total": len(tasks),
    }
    meta.write_text(json.dumps(meta_rec, ensure_ascii=False, indent=2), encoding="utf-8")

    with raw.open("w", encoding="utf-8") as f:
        for t in tasks:
            rec = run_one(t, args.cmd, args.seat, args.arm)
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print("%s %s %s" % (rec["task_id"], rec["outcome"], rec.get("reason", "")))

    print("\n写入 %s" % raw)
    return 0


if __name__ == "__main__":
    sys.exit(main())
