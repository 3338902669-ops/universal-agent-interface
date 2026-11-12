#!/usr/bin/env python3
"""Read a raw run directory and emit the five E-02 metrics.

If the raw directory is missing, empty, or not a real run, exit non-zero
and do not invent numbers.
"""

from __future__ import annotations

import argparse
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

    trials = load_trials(raw)
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

    freeze = {}
    freeze_path = raw / "freeze.json"
    if freeze_path.is_file():
        freeze = json.loads(freeze_path.read_text(encoding="utf-8"))

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
