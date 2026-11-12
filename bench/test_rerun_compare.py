"""E-04 自检：复跑比对器必须有牙。

关键断言：
  1. 完全一致 → equivalent / exit 0；
  2. 判据名不同 → divergent / exit 1（不是"再绿一次"就算数）；
  3. 退出码不同 → divergent；
  4. 套件缺失 → divergent；
  5. 只有易噪细节不同（pid/耗时/临时路径）→ 仍判 equivalent（不误杀）；
  6. 一侧 skipped、另一侧真跑 → divergent（不能拿 skip 冒充通过）。
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import compare_reruns as C  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-52s %s" % ("PASS" if cond else "FAIL", name, detail))


def write(d: Path, suites: list[dict]) -> str:
    # 每次写不同文件：同名会互相覆盖，导致「拿同一份清单和自己比」这种假绿。
    p = d / ("rerun-%03d.json" % len(list(d.glob("rerun-*.json"))))
    p.write_text(json.dumps({"schema": "uai.bench.rerun/1", "suites": suites, "failed": []}), encoding="utf-8")
    return str(p)


def row(name, exit=0, signal=None, skipped=False, reason=None):
    r = {"name": name, "exit": exit, "signal": signal if signal is not None else ["PASS  a"]}
    if skipped:
        r = {"name": name, "skipped": True, "reason": reason or "requires_lab"}
    return r


def main() -> int:
    d = Path(tempfile.mkdtemp(prefix="rerun-cmp-"))

    a = write(d, [row("s1", signal=["PASS  x    pid=1"]), row("s2")])
    b = write(d, [row("s1", signal=["PASS  x    pid=2"]), row("s2")])
    check("E-04 细节噪声不同 → 仍 equivalent", C.main([a, b]) == 0)

    b2 = write(d, [row("s1", signal=["FAIL  x"]), row("s2")])
    check("E-04 判据名不同 → divergent", C.main([a, b2]) == 1)

    b3 = write(d, [row("s1", exit=1), row("s2")])
    check("E-04 退出码不同 → divergent", C.main([a, b3]) == 1)

    b4 = write(d, [row("s1")])
    check("E-04 套件缺失 → divergent", C.main([a, b4]) == 1)

    a5 = write(d, [row("s1", skipped=True)])
    b5 = write(d, [row("s1", signal=["PASS  x"])])
    check("E-04 一侧 skip 一侧真跑 → divergent", C.main([a5, b5]) == 1)

    a6 = write(d, [row("s1", skipped=True), row("s2")])
    b6 = write(d, [row("s1", skipped=True), row("s2")])
    check("E-04 两侧都 skip 且理由一致 → equivalent", C.main([a6, b6]) == 0)

    check("E-04 归一化保留判据名", C.normalize_signal("PASS  A-09 拒绝时能报出持有者 pid  41248") ==
          "PASS A-09 拒绝时能报出持有者 pid", C.normalize_signal("PASS  A-09 拒绝时能报出持有者 pid  41248"))

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
