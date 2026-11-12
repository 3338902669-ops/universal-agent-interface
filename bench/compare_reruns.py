"""E-04 复跑比对：两份独立清单必须逐套件等价。

判「等价」的口径：
  * 套件集合相同；
  * 每个套件退出码相同；
  * 每个套件的语义信号（PASS/FAIL 名称、计数、RESULT 行）摘要相同。

不比较耗时、临时路径、绝对路径、日志顺序噪声。
任一项不等价 → 打印差异并以非 0 退出，CI 该 job 判红。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# 判据名之后常跟着易噪细节（pid、临时路径、时间戳、耗时）。
# 等价性只认「状态 + 判据名」，细节留在原始清单里备查。
_DETAIL_SPLIT = re.compile(r"\s{2,}")
_STATUS = re.compile(r"^(PASS|FAIL)\b\s*(.*)$")


def normalize_signal(line: str) -> str:
    s = line.strip()
    m = _STATUS.match(s)
    if m:
        name = _DETAIL_SPLIT.split(m.group(2))[0].strip()
        return "%s %s" % (m.group(1), name)
    return s


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("a")
    ap.add_argument("b")
    ap.add_argument("--out", help="把比对结果写成 JSON")
    args = ap.parse_args(argv)

    a, b = load(args.a), load(args.b)
    ia = {r["name"]: r for r in a.get("suites") or []}
    ib = {r["name"]: r for r in b.get("suites") or []}

    diffs: list[dict] = []
    for name in sorted(set(ia) | set(ib)):
        ra, rb = ia.get(name), ib.get(name)
        if ra is None or rb is None:
            diffs.append({"suite": name, "kind": "missing_set", "a": ra is not None, "b": rb is not None})
            continue
        if ra.get("skipped") or rb.get("skipped"):
            if bool(ra.get("skipped")) != bool(rb.get("skipped")):
                diffs.append({"suite": name, "kind": "skip_mismatch"})
            continue
        if ra.get("exit") != rb.get("exit"):
            diffs.append({"suite": name, "kind": "exit_mismatch", "a": ra.get("exit"), "b": rb.get("exit")})
        else:
            sa = [normalize_signal(x) for x in ra.get("signal") or []]
            sb = [normalize_signal(x) for x in rb.get("signal") or []]
            if sa != sb:
                only_a = [x for x in sa if x not in sb]
                only_b = [x for x in sb if x not in sa]
                diffs.append({"suite": name, "kind": "signal_mismatch",
                              "only_a": only_a[:10], "only_b": only_b[:10],
                              "count_a": len(sa), "count_b": len(sb)})

    result = {
        "verdict": "equivalent" if not diffs else "divergent",
        "suites_compared": len(set(ia) | set(ib)),
        "divergent": diffs,
        "a_failed": a.get("failed"),
        "b_failed": b.get("failed"),
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if not diffs else 1


if __name__ == "__main__":
    sys.exit(main())
