"""Pure scoring functions for the E-02 controlled experiment.

Same (output, scoring spec, answer) must always produce the same score.
These functions have no I/O, no clock, and no hidden RNG.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional


def _normalize(text: str, ops: Optional[list] = None) -> str:
    out = "" if text is None else str(text)
    for op in ops or []:
        if op == "strip":
            out = out.strip()
        elif op == "collapse_ws":
            out = re.sub(r"\s+", " ", out).strip()
        elif op == "lower":
            out = out.lower()
        elif op == "upper":
            out = out.upper()
        else:
            raise ValueError("unknown normalize op: %s" % op)
    return out


def exact_match(output: str, answer: str, scoring: Mapping[str, Any]) -> int:
    ops = list(scoring.get("normalize") or ["strip", "collapse_ws"])
    return int(_normalize(output, ops) == _normalize(answer, ops))


def set_csv(output: str, answer: str, scoring: Mapping[str, Any]) -> int:
    """Order-independent comma-separated token match."""
    ops = list(scoring.get("normalize") or ["strip", "lower"])
    sep = scoring.get("sep", ",")

    def tokens(s: str) -> set:
        parts = [_normalize(p, ops) for p in str(s).split(sep)]
        return {p for p in parts if p}

    return int(tokens(output) == tokens(answer))


def sha256_hex(output: str, answer: str, scoring: Mapping[str, Any]) -> int:
    ops = list(scoring.get("normalize") or ["strip", "lower"])
    got = _normalize(output, ops)
    exp = _normalize(answer, ops)
    if not re.fullmatch(r"[0-9a-f]{64}", got or ""):
        return 0
    return int(got == exp)


SCORERS = {
    "exact_match": exact_match,
    "set_csv": set_csv,
    "sha256_hex": sha256_hex,
}


def score(output: str, task: Mapping[str, Any]) -> int:
    """Score a model/stub output against a task. Pure: no I/O."""
    spec = task.get("scoring")
    if not isinstance(spec, dict) or "fn" not in spec:
        raise ValueError("task %s is missing scoring.fn" % task.get("id"))
    fn = SCORERS.get(spec["fn"])
    if fn is None:
        raise ValueError("unknown scorer %r on task %s" % (spec["fn"], task.get("id")))
    return int(fn(output, task["answer"], spec))
