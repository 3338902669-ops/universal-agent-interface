"""E-02 真实两档对照：直接以 argv 列表调用 codex，不经过 shell。

为什么不复用 run.py 的 --cmd 模板：Windows 下 `shell=True` 会把提示词按空格/引号
重新切分，中文提示词被吞掉（实测模型反问"要我回复什么"）。argv 列表没有这个问题，
而且能保证两档**逐字同一个提示词**——这正是受控实验的前提。

判分器不重写：直接 import run.score（冻结件），保证跨档同源。
元数据格式与 run.py 一致，analyze.py 可直接汇总。

用法：
    python bench/controlled_experiment/run_pair.py --tasks 6 --out raw
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import run as R  # noqa: E402  复用冻结判分器与 sha256 工具

CODEX = (r"C:\Users\33389\AppData\Roaming\npm\node_modules\@openai\codex"
         r"\node_modules\@openai\codex-win32-x64\vendor\x86_64-pc-windows-msvc\bin\codex.exe")
HOMES = {
    "with_skill": r"C:\Users\33389\.codex",
    "without_skill": r"C:\Users\33389\workspace\scratch\ce-home-lite",
}


def kill_tree(proc: subprocess.Popen) -> None:
    """杀掉整棵进程树。

    为什么不能只 proc.kill()：Windows 上 kill() 只终止直接子进程，而 codex 会派生
    子进程并继承 stdout/stderr 管道句柄；communicate() 要等**所有**持有该句柄的进程退出，
    于是「配置 1200s 超时、实测 1828.9s 才返回」——超时没有约束住墙钟时间（E1 实测）。
    """
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                       capture_output=True)
    else:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except Exception:
            proc.kill()
    try:
        proc.wait(timeout=30)
    except Exception:
        pass


def run_one(task: dict, arm: str, workdir: Path) -> dict:
    timeout = int(task.get("timeout_s", 1200))
    env = dict(os.environ)
    env["CODEX_HOME"] = HOMES[arm]
    argv = [CODEX, "exec", "--skip-git-repo-check", "--sandbox", "read-only",
            "--ephemeral", "-C", str(workdir), task["prompt"]]

    started = time.time()
    rec = {"task_id": task["id"], "seat": "codex-cli", "arm": arm,
           "started_at": started, "timeout_s": timeout}
    popen_kw = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL, env=env, text=True,
                    encoding="utf-8", errors="replace")
    if os.name != "nt":
        popen_kw["start_new_session"] = True      # 便于整组回收
    try:
        proc = subprocess.Popen(argv, **popen_kw)
    except FileNotFoundError as e:
        rec["outcome"] = "channel_failure"
        rec["reason"] = "command_not_found:%s" % e
        return rec

    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=30)
        except subprocess.TimeoutExpired:
            out, err = "", ""
        rec["exit_code"] = proc.returncode
        rec["stdout"] = (out or "")[-4000:]
        rec["stderr"] = (err or "")[-2000:]
        rec["outcome"] = "channel_failure"
        rec["reason"] = "timeout"
        rec["duration_s"] = round(time.time() - started, 3)
        return rec

    rec["exit_code"] = proc.returncode
    rec["stdout"] = (out or "")[-4000:]
    rec["stderr"] = (err or "")[-2000:]
    rec["duration_s"] = round(time.time() - started, 3)
    if proc.returncode != 0 and not (out or "").strip():
        rec["outcome"] = "channel_failure"
        rec["reason"] = "nonzero_exit_no_output"
        return rec
    ok, why = R.score(task, out or "")
    rec["outcome"] = "pass" if ok else "capability_failure"
    rec["reason"] = why
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=6, help="只跑前 N 道（默认全部 6 道）")
    ap.add_argument("--out", default=str(HERE / "raw"))
    ap.add_argument("--arm", choices=["with_skill", "without_skill", "both"], default="both")
    args = ap.parse_args(argv)

    tasks = R.load_tasks()[: args.tasks]
    if not tasks:
        print("no_tasks", file=sys.stderr)
        return 2

    arms = ["with_skill", "without_skill"] if args.arm == "both" else [args.arm]
    out_root = Path(args.out)
    workdir = out_root / "_work"
    workdir.mkdir(parents=True, exist_ok=True)

    for arm in arms:
        d = out_root / arm
        d.mkdir(parents=True, exist_ok=True)
        meta = {
            "seat": "codex-cli", "arm": arm,
            "cmd_template": "argv-list (no shell)",
            "codex_home": HOMES[arm],
            "codex_version": "0.147.0",
            "model": "deepseek-flash", "model_provider": "deepseek",
            "sandbox": "read-only", "ephemeral": True,
            "fixture_sha256": R.file_sha256(HERE / "fixtures" / "tasks.jsonl"),
            "scoring_frozen_sha256": R.file_sha256(HERE / "run.py"),
            "python": sys.version.split()[0],
            "started_at": time.time(), "tasks_total": len(tasks),
        }
        (d / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        with (d / "attempts.jsonl").open("w", encoding="utf-8") as f:
            for t in tasks:
                rec = run_one(t, arm, workdir)
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print("%-14s %-5s %-16s %s" % (arm, rec["task_id"], rec["outcome"],
                                               str(rec.get("reason", ""))[:50]))
        print("-> %s" % (d / "attempts.jsonl"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
