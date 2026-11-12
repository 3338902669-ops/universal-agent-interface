"""E-02 实验包清单：把「哪一版判分器 / 哪一版题目 / 哪个解释器」钉死。

用途：
  1. 跑实验前用它生成 MANIFEST.json，随结果一起提交；
  2. 复跑时用 `--verify MANIFEST.json` 比对，不一致即拒跑（不可比的实验不是实验）。

只读文件、只读解释器信息，不联网、不读时钟进哈希。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PINNED = ["fixtures/tasks.jsonl", "run.py", "run_pair.py", "analyze.py", "test_controlled.py"]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build() -> dict:
    files = {}
    for rel in PINNED:
        p = HERE / rel
        files[rel] = {"sha256": sha256_file(p), "bytes": p.stat().st_size} if p.exists() else {"missing": True}
    tasks = 0
    fx = HERE / "fixtures" / "tasks.jsonl"
    if fx.exists():
        tasks = sum(1 for line in fx.read_text(encoding="utf-8").splitlines() if line.strip())
    return {
        "schema": "uai.bench.controlled_experiment.manifest/1",
        "files": files,
        "tasks": tasks,
        "python": {"implementation": platform.python_implementation(), "version": platform.python_version()},
        "platform": {"system": platform.system(), "release": platform.release(), "machine": platform.machine()},
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE / "MANIFEST.json"))
    ap.add_argument("--verify", metavar="MANIFEST", help="与已有清单比对，不一致则非 0 退出")
    args = ap.parse_args(argv)

    cur = build()
    if args.verify:
        old = json.loads(Path(args.verify).read_text(encoding="utf-8-sig"))
        diffs = []
        for rel, meta in (old.get("files") or {}).items():
            if cur["files"].get(rel) != meta:
                diffs.append(rel)
        if diffs or old.get("tasks") != cur["tasks"]:
            print(json.dumps({"verdict": "not_reproducible", "differing": diffs, "tasks": cur["tasks"]}, ensure_ascii=False))
            return 2
        print(json.dumps({"verdict": "reproducible", "tasks": cur["tasks"]}, ensure_ascii=False))
        return 0

    Path(args.out).write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": "written", "out": args.out, "tasks": cur["tasks"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
