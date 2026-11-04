#!/usr/bin/env python3
"""Provision lab-only test tokens; never print token values."""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path

from core import atomic_write_json, token_digest


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("--interface-id", action="append", required=True)
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    base = args.base.resolve()
    token_dir = base / ".lab-tokens"
    token_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = base / "data" / "token-hashes.json"
    catalog: dict[str, str] = {}
    if catalog_path.exists():
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    created: list[str] = []
    kept: list[str] = []
    for interface_id in args.interface_id:
        if not interface_id or any(ch in interface_id for ch in "\\/:"):
            raise ValueError("interface id must be a simple lab identifier")
        token_path = token_dir / f"{interface_id}.token"
        if token_path.exists() and not args.rotate:
            token = token_path.read_text(encoding="utf-8").strip()
            kept.append(interface_id)
        else:
            token = secrets.token_urlsafe(32)
            token_path.write_text(token + "\n", encoding="utf-8", newline="\n")
            created.append(interface_id)
        catalog[interface_id] = token_digest(token)
    atomic_write_json(catalog_path, catalog)
    print(
        json.dumps(
            {
                "created_count": len(created),
                "kept_count": len(kept),
                "created_ids": created,
                "token_values_printed": False,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
