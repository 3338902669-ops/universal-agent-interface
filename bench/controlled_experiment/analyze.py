"""受控对照实验：分析器（E-02）。

硬要求：没有数据时必须非 0 退出并打印 no_data——不允许"空目录 → 0 失败 → 通过率 100%"。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

CHANNEL_NOISE_THRESHOLD = 0.20


def load_attempts(raw: Path) -> list[dict]:
    p = raw / "attempts.jsonl"
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def load_meta(raw: Path) -> dict:
    p = raw / "meta.json"
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def summarize(raw: Path) -> dict:
    attempts = load_attempts(raw)
    meta = load_meta(raw)
    if not attempts:
        return {"verdict": "no_data", "raw": str(raw)}

    total = len(attempts)
    passed = sum(1 for a in attempts if a.get("outcome") == "pass")
    cap = sum(1 for a in attempts if a.get("outcome") == "capability_failure")
    chan = sum(1 for a in attempts if a.get("outcome") == "channel_failure")
    durations = [a.get("duration_s") for a in attempts if isinstance(a.get("duration_s"), (int, float))]

    scored = total - chan
    pass_rate = round(passed / scored, 4) if scored else 0.0
    chan_rate = round(chan / total, 4) if total else 0.0

    verdict = "valid"
    if chan_rate > CHANNEL_NOISE_THRESHOLD:
        verdict = "invalid_channel_noise"

    return {
        "raw": str(raw),
        "seat": meta.get("seat"),
        "arm": meta.get("arm"),
        "tasks_total": total,
        "attempted": total,
        "pass": passed,
        "capability_failure": cap,
        "channel_failure": chan,
        "pass_rate": pass_rate,
        "channel_failure_rate": chan_rate,
        "capability_failure_rate": round(cap / total, 4) if total else 0.0,
        "mean_duration_s": round(sum(durations) / len(durations), 2) if durations else None,
        "fixture_sha256": meta.get("fixture_sha256"),
        "scoring_frozen_sha256": meta.get("scoring_frozen_sha256"),
        "verdict": verdict,
    }


def compare(a: dict, b: dict) -> dict:
    same_fixture = a.get("fixture_sha256") == b.get("fixture_sha256")
    same_scoring = a.get("scoring_frozen_sha256") == b.get("scoring_frozen_sha256")
    comparable = bool(same_fixture and same_scoring
                      and a.get("verdict") == "valid" and b.get("verdict") == "valid")
    return {
        "comparable": comparable,
        "same_fixture": same_fixture,
        "same_scoring": same_scoring,
        "arms": [a.get("arm"), b.get("arm")],
        "pass_rate_delta": round((a.get("pass_rate") or 0) - (b.get("pass_rate") or 0), 4),
        "note": ("可比" if comparable else
                 "不可比：fixture/判分器不同，或某档通道噪声超阈值"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", help="单个 run 目录")
    ap.add_argument("--compare", nargs=2, metavar=("RAW_A", "RAW_B"))
    ap.add_argument("--allow-empty", action="store_true",
                    help="仅用于自检；生产分析不得使用")
    args = ap.parse_args(argv)

    if args.compare:
        a = summarize(Path(args.compare[0]))
        b = summarize(Path(args.compare[1]))
        if a["verdict"] == "no_data" or b["verdict"] == "no_data":
            print(json.dumps({"verdict": "no_data",
                              "raw_a": a["verdict"], "raw_b": b["verdict"]}, ensure_ascii=False))
            return 3
        print(json.dumps({"a": a, "b": b, "compare": compare(a, b)},
                         ensure_ascii=False, indent=2))
        return 0

    if not args.raw:
        print("需要 --raw 或 --compare", file=sys.stderr)
        return 2

    s = summarize(Path(args.raw))
    if s["verdict"] == "no_data" and not args.allow_empty:
        print(json.dumps({"verdict": "no_data", "raw": s["raw"]}, ensure_ascii=False))
        print("no_data：raw 目录里没有 attempts.jsonl（空目录不算 0 失败）", file=sys.stderr)
        return 4

    print(json.dumps(s, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
