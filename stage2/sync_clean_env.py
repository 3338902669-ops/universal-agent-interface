"""同步 prototype -> prototype_clean（端到端测试环境）。

端到端验证用 prototype_clean 是为了避免污染真实账本，
但这也意味着改了 prototype 之后必须同步，否则 e2e 跑的是旧代码。

用法：
    python stage2/sync_clean_env.py          # 同步代码文件
    python stage2/sync_clean_env.py --reset  # 同步并清空账本
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "prototype"
DST = REPO / "prototype_clean"

CODE_FILES = [
    "hub.py", "interface.py", "core.py", "ledger_seal.py",
    "lab_client.py", "provision_tokens.py",
]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="同步后清空账本")
    args = ap.parse_args()

    if not SRC.is_dir():
        print("source missing: %s" % SRC)
        return 1
    DST.mkdir(parents=True, exist_ok=True)

    synced = []
    for name in CODE_FILES:
        s = SRC / name
        if not s.is_file():
            print("skip (missing): %s" % name)
            continue
        shutil.copyfile(s, DST / name)
        synced.append(name)

    # 静态资源
    for name in ("nats-test.conf",):
        s = SRC / name
        if s.is_file():
            shutil.copyfile(s, DST / name)
            synced.append(name)

    # 含敏感文件的目录不同步；只确保目录存在
    for d in ("data", ".lab-tokens", "logs", "runtime"):
        (DST / d).mkdir(parents=True, exist_ok=True)

    if args.reset:
        ledger = DST / "data" / "ledger.jsonl"
        if ledger.exists():
            ledger.unlink()
            print("ledger reset")

    print("synced %d files: %s" % (len(synced), ", ".join(synced)))
    print("note: runtime_adapters / echo_adapter 等测试专用适配器需单独放置")
    return 0


if __name__ == "__main__":
    sys.exit(main())
