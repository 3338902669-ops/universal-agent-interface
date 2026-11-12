#!/usr/bin/env python3
"""One-shot runner for the E-02 controlled contrast experiment.

Writes a new directory under raw/ plus summary.json. Does not invent
model results: the default backend is a deterministic stub. A live HTTP
backend is used only when explicitly requested and the API key is present
in the environment; otherwise the runner refuses rather than faking scores.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scoring  # noqa: E402

CONDITIONS = ("without_orchestration", "with_orchestration")

# Frozen skill pack (protocol excerpts, not a labelled answer key).
# The with_orchestration stub may read this; the without_orchestration stub may not.
SKILL_PACK = {
    "id": "uai-protocol-skill-pack-v1",
    "excerpts": {
        "verdicts": "六值裁定：通过、带注通过、条件通过、不通过、未核、作废。带注通过 = 核验通过但附有注记。",
        "a2a_projection": "QUEUED / DISPATCHED → TASK_STATE_SUBMITTED；CLAIMED / RUNNING → TASK_STATE_WORKING。",
        "evidence_e1": "E1 需要 command、exit_code、revision。E2 需要 peer。",
        "floor": "rigor 与 cls 正交。只读的 L3 仍须 E1，不允许仅用 E3 收口。",
        "governance_bridge": "SUCCEEDED + 未核 → governance_state = blocked。",
        "sha256_note": "T02 要求对给定 UTF-8 字符串做 SHA-256，输出小写 64 位十六进制。",
    },
}

# Incomplete knowledge used only by the without_orchestration stub.
# Deliberately wrong or missing on some items so the contrast is visible.
STUB_BASELINE = {
    "T01": "带注通过",
    "T02": "deadbeef",          # capability failure: not a 64-hex digest of the prompt string
    "T03": "TASK_STATE_SUBMITTED",
    "T04": "command,revision",  # capability failure: missing exit_code
    "T05": "no",
    "T06": None,                # channel failure: stub pretends the session dropped
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_tasks(path: Path) -> list[dict[str, Any]]:
    tasks = []
    with io.open(path, encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            item = json.loads(line)
            if "scoring" not in item:
                raise SystemExit("tasks.jsonl line %d is missing scoring" % line_no)
            tasks.append(item)
    if len(tasks) != 6:
        raise SystemExit("expected 6 tasks, found %d" % len(tasks))
    return tasks


def git_sha() -> Optional[str]:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(HERE.parents[1]),
            stderr=subprocess.DEVNULL,
        )
        return out.decode("ascii").strip()
    except Exception:
        return None


def classify_channel_error(error_code: Optional[str]) -> bool:
    return error_code in {
        "timeout",
        "http_error",
        "auth_error",
        "connection_error",
        "backend_crash",
        "empty_channel",
        "missing_key",
    }


def stub_answer(task: dict[str, Any], condition: str) -> dict[str, Any]:
    """Deterministic stub. Same task+condition always yields the same payload."""
    if condition == "with_orchestration":
        # Skill pack lets the stub retrieve the protocol fact or compute T02.
        if task["id"] == "T02":
            target = "universal-agent-interface/e-02/t02"
            output = hashlib.sha256(target.encode("utf-8")).hexdigest()
        elif task["id"] == "T01":
            output = "带注通过"
        elif task["id"] == "T03":
            output = "TASK_STATE_SUBMITTED"
        elif task["id"] == "T04":
            output = "command,exit_code,revision"
        elif task["id"] == "T05":
            output = "no"
        elif task["id"] == "T06":
            output = "blocked"
        else:
            output = ""
        return {"output": output, "error_code": None, "error": None}

    value = STUB_BASELINE.get(task["id"], "")
    if value is None:
        return {
            "output": "",
            "error_code": "empty_channel",
            "error": "stub: session dropped before a scorable output arrived",
        }
    return {"output": value, "error_code": None, "error": None}


def http_answer(task: dict[str, Any], condition: str, url: str, key: str, model: str, timeout_s: float) -> dict[str, Any]:
    """Optional live backend. Key is read from the environment only and never written out."""
    skill = ""
    if condition == "with_orchestration":
        skill = "You may use this frozen skill pack:\n" + json.dumps(SKILL_PACK, ensure_ascii=False)
    prompt = task["prompt"]
    if skill:
        user = skill + "\n\n" + prompt
    else:
        user = prompt
    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "Answer with only the requested token or list. No preamble."},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            raw = resp.read().decode("utf-8")
        data = json.loads(raw)
        # OpenAI-style or a plain {"output": "..."} payload.
        output = ""
        if isinstance(data, dict):
            if "output" in data:
                output = str(data["output"])
            else:
                try:
                    output = data["choices"][0]["message"]["content"]
                except Exception:
                    output = raw
        return {"output": output, "error_code": None, "error": None}
    except urllib.error.HTTPError as e:
        code = "auth_error" if e.code in (401, 403) else "http_error"
        return {"output": "", "error_code": code, "error": "HTTP %s" % e.code}
    except urllib.error.URLError as e:
        return {"output": "", "error_code": "connection_error", "error": str(e.reason)}
    except TimeoutError:
        return {"output": "", "error_code": "timeout", "error": "timeout"}


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "run-%s-%s" % (stamp, uuid.uuid4().hex[:8])


def write_json(path: Path, obj: Any) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks)
    # Parse, hash, and persist the same immutable byte snapshot.
    task_bytes = tasks_path.read_bytes()
    tasks = [json.loads(line) for line in task_bytes.decode("utf-8-sig").splitlines() if line.strip()]
    if len(tasks) != 6 or any("scoring" not in task for task in tasks):
        raise SystemExit("expected six tasks with scoring")
    backend = args.backend
    key = os.environ.get("UAI_EXPERIMENT_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
    url = args.url or os.environ.get("UAI_EXPERIMENT_URL") or ""
    model = args.model or os.environ.get("UAI_EXPERIMENT_MODEL") or "unspecified"

    if backend == "http":
        if not key:
            print("REFUSING: backend=http but UAI_EXPERIMENT_API_KEY / OPENAI_API_KEY is unset. Not writing fake results.", file=sys.stderr)
            return 2
        if not url:
            print("REFUSING: backend=http but --url / UAI_EXPERIMENT_URL is unset.", file=sys.stderr)
            return 2

    run_id = args.run_id or new_run_id()
    out_dir = Path(args.out) / run_id
    if out_dir.exists():
        print("REFUSING: %s already exists; will not overwrite." % out_dir, file=sys.stderr)
        return 2
    out_dir.mkdir(parents=True)

    freeze = {
        "run_id": run_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "model": model if backend == "http" else "stub-deterministic",
        "conditions": list(CONDITIONS),
        "tasks_path": str(tasks_path),
        "tasks_sha256": hashlib.sha256(task_bytes).hexdigest(),
        "scoring_sha256": sha256_file(HERE / "scoring.py"),
        "skill_pack_sha256": sha256_text(json.dumps(SKILL_PACK, ensure_ascii=False, sort_keys=True)),
        "git_sha": git_sha(),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "timeout_s": args.timeout,
        "api_key_present": bool(key) if backend == "http" else False,
        "url_present": bool(url) if backend == "http" else False,
    }
    # Preserve the exact task bytes used; a later checkout may change line endings.
    (out_dir / "tasks.frozen.jsonl").write_bytes(task_bytes)
    write_json(out_dir / "freeze.json", freeze)

    trials = []
    for condition in CONDITIONS:
        for task in tasks:
            t0 = time.perf_counter()
            if backend == "stub":
                payload = stub_answer(task, condition)
            else:
                payload = http_answer(task, condition, url, key, model, args.timeout)
            elapsed = time.perf_counter() - t0
            channel_failure = classify_channel_error(payload.get("error_code"))
            item_score = None
            if not channel_failure:
                item_score = scoring.score(payload.get("output") or "", task)
            trial = {
                "task_id": task["id"],
                "condition": condition,
                "output": payload.get("output") or "",
                "score": item_score,
                "channel_failure": channel_failure,
                "capability_failure": (item_score == 0) if item_score is not None else False,
                "error_code": payload.get("error_code"),
                "error": payload.get("error"),
                "duration_s": round(elapsed, 6),
            }
            trials.append(trial)

    trials_path = out_dir / "trials.jsonl"
    with trials_path.open("w", encoding="utf-8") as f:
        for trial in trials:
            f.write(json.dumps(trial, ensure_ascii=False, separators=(",", ":")) + "\n")

    # Import analyze lazily so a raw tree is already on disk if analysis fails.
    import analyze  # noqa: E402

    summary = analyze.summarize(out_dir)
    write_json(out_dir / "summary.json", summary)

    print("RESULT: wrote %s trials=%d" % (out_dir, len(trials)))
    print(json.dumps(summary["metrics"], ensure_ascii=False, indent=2))
    print("RUN_DIR=%s" % out_dir)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="E-02 controlled contrast runner")
    p.add_argument("--backend", choices=("stub", "http"), default="stub")
    p.add_argument("--tasks", default=str(HERE / "fixtures" / "tasks.jsonl"))
    p.add_argument("--out", default=str(HERE / "raw"), help="parent directory for run folders")
    p.add_argument("--run-id", default=None)
    p.add_argument("--timeout", type=float, default=120.0)
    p.add_argument("--url", default=None, help="live backend URL; otherwise UAI_EXPERIMENT_URL")
    p.add_argument("--model", default=None, help="live model name; otherwise UAI_EXPERIMENT_MODEL")
    args = p.parse_args()
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
