#!/usr/bin/env python3
"""Read a raw run directory and emit the five E-02 metrics.

If the raw directory is missing, empty, or not a real run, exit non-zero
and do not invent numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import scoring  # noqa: E402


METRIC_NAMES = (
    "channel_failure_rate",
    "capability_failure_rate",
    "exact_match_rate",
    "mean_score",
    "orchestration_delta",
)


def _err(msg: str, code: int = 1) -> int:
    print("ERROR: %s" % msg, file=sys.stderr)
    print("RESULT: no metrics; refusing to invent results", file=sys.stderr)
    return code


def load_trials(raw: Path) -> list[dict[str, Any]]:
    path = raw / "trials.jsonl"
    if not path.is_file():
        raise FileNotFoundError("missing trials.jsonl")
    trials = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            trials.append(json.loads(line))
    if not trials:
        raise ValueError("trials.jsonl is empty")
    return trials


def rate(numer: int, denom: int) -> Optional[float]:
    if denom == 0:
        return None
    return numer / denom


def metrics_for(trials: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(trials)
    channel = [t for t in trials if t.get("channel_failure")]
    scored = [t for t in trials if not t.get("channel_failure")]
    cap_fail = [t for t in scored if t.get("score") == 0]
    exact = [t for t in scored if t.get("score") == 1]
    mean = None
    if scored:
        mean = sum(float(t["score"]) for t in scored) / len(scored)
    return {
        "n_trials": n,
        "n_channel_failure": len(channel),
        "n_scored": len(scored),
        "n_capability_failure": len(cap_fail),
        "n_exact_match": len(exact),
        "channel_failure_rate": rate(len(channel), n),
        "capability_failure_rate": rate(len(cap_fail), len(scored)),
        "exact_match_rate": rate(len(exact), len(scored)),
        "mean_score": mean,
    }


def summarize(raw: Path) -> dict[str, Any]:
    raw = Path(raw)
    if not raw.exists():
        raise FileNotFoundError("raw path does not exist: %s" % raw)
    if not raw.is_dir():
        raise NotADirectoryError("raw path is not a directory: %s" % raw)

    freeze_path = raw / "freeze.json"
    if not freeze_path.is_file():
        raise ValueError("missing freeze.json")
    freeze = json.loads(freeze_path.read_text(encoding="utf-8"))
    tasks_path = raw / "tasks.frozen.jsonl"
    if not tasks_path.is_file():
        # Legacy runs contain no snapshot; use the shipped fixture only if its
        # exact bytes (or LF-normalized checkout) match the recorded digest.
        tasks_path = HERE / "fixtures" / "tasks.jsonl"
    scoring_path = HERE / "scoring.py"
    for key, path in (("tasks_sha256", tasks_path), ("scoring_sha256", scoring_path)):
        data = path.read_bytes()
        digests = {hashlib.sha256(data).hexdigest()}
        if path.parent == HERE or path.parent == HERE / "fixtures":
            digests.add(hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest())
        if freeze.get(key) not in digests:
            raise ValueError("frozen %s does not match artifact" % key)
    tasks = [json.loads(line) for line in tasks_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    by_id = {task["id"]: task for task in tasks}
    if len(by_id) != len(tasks) or not tasks:
        raise ValueError("invalid task fixture")
    trials = load_trials(raw)
    expected = {(condition, task_id) for condition in ("without_orchestration", "with_orchestration") for task_id in by_id}
    seen = set()
    for trial in trials:
        key = (trial.get("condition"), trial.get("task_id"))
        if key not in expected or key in seen:
            raise ValueError("unknown or duplicate trial: %r" % (key,))
        seen.add(key)
        channel = trial.get("channel_failure")
        if not isinstance(channel, bool):
            raise ValueError("channel_failure must be boolean")
        if channel:
            if trial.get("score") is not None or not trial.get("error_code"):
                raise ValueError("channel failure must have an error code and no score")
        elif trial.get("error_code") is not None:
            raise ValueError("scored trial cannot carry a channel error")
        elif trial.get("score") != scoring.score(trial.get("output") or "", by_id[key[1]]):
            raise ValueError("recorded score does not match frozen scorer")
    if seen != expected:
        raise ValueError("incomplete trials: missing %d" % len(expected - seen))
    overall = metrics_for(trials)
    by_condition = {}
    for cond in ("without_orchestration", "with_orchestration"):
        subset = [t for t in trials if t.get("condition") == cond]
        by_condition[cond] = metrics_for(subset) if subset else {
            "n_trials": 0,
            "n_channel_failure": 0,
            "n_scored": 0,
            "n_capability_failure": 0,
            "n_exact_match": 0,
            "channel_failure_rate": None,
            "capability_failure_rate": None,
            "exact_match_rate": None,
            "mean_score": None,
        }

    with_r = by_condition["with_orchestration"]["exact_match_rate"]
    without_r = by_condition["without_orchestration"]["exact_match_rate"]
    delta = None if (with_r is None or without_r is None) else (with_r - without_r)

    return {
        "raw": str(raw),
        "freeze": freeze,
        "metrics": {
            "channel_failure_rate": overall["channel_failure_rate"],
            "capability_failure_rate": overall["capability_failure_rate"],
            "exact_match_rate": overall["exact_match_rate"],
            "mean_score": overall["mean_score"],
            "orchestration_delta": delta,
        },
        "overall": overall,
        "by_condition": by_condition,
        "n_trials": overall["n_trials"],
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Analyze one E-02 raw run")
    p.add_argument("--raw", required=True, help="path to a single raw/<run> directory")
    args = p.parse_args()
    raw = Path(args.raw)
    try:
        summary = summarize(raw)
    except (FileNotFoundError, NotADirectoryError, ValueError, json.JSONDecodeError) as e:
        return _err(str(e), 1)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    m = summary["metrics"]
    print(
        "RESULT: M1_channel=%.4f M2_capability=%s M3_exact=%s M4_mean=%s M5_delta=%s"
        % (
            m["channel_failure_rate"] if m["channel_failure_rate"] is not None else float("nan"),
            "null" if m["capability_failure_rate"] is None else "%.4f" % m["capability_failure_rate"],
            "null" if m["exact_match_rate"] is None else "%.4f" % m["exact_match_rate"],
            "null" if m["mean_score"] is None else "%.4f" % m["mean_score"],
            "null" if m["orchestration_delta"] is None else "%.4f" % m["orchestration_delta"],
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
