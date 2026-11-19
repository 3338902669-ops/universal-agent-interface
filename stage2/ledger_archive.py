"""UAI-09：把历史心跳从主账本里"归档出去"——可验证，不改历史。

问题（实测 2026-11-17，团队 base 账本）：
    5220 行里有 **5000 行是 INTERFACE_HEARTBEAT**，业务记录只有 220 行。
    769 行开场全是心跳，中间一整块连着 4177 行，剩下 188 行业务在末尾。
    账本因此几乎没法读——这才是 UAI-09 要解决的"可读性"，不是容量。

三条不可退让的约束：
  1. **不改历史**。账本是只追加 + 哈希链，删中间行会让链断掉，那就不再是账本。
     所以本模块**从不修改源文件**：归档是"拷贝出去 + 留清单"。
  2. **可验证**。归档出来的字节必须与源文件**逐字节相同**（不做 JSON 往返），
     清单里记源文件 sha256 / 行数 / 每段桥接的 self_hash，事后能证明
     "搬走的确实是这些行、源文件这一段没被改过"。
  3. **压缩是另存一份**。compact 只**新建**一个文件（拒绝覆盖），并在末尾追加一条
     COMPACTION 记录把源文件指纹与清单绑在一起；链的不连续点必须全部出现在清单的
     bridges 里，否则 verify_compacted 判红。是否切换读者到压缩版是运维决定，
     不是本模块替用户做的决定。
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from pathlib import Path

ARCHIVE_SCHEMA = "uai.ledger-archive/v1"
DEFAULT_KINDS = ("INTERFACE_HEARTBEAT",)
GENESIS = "0" * 64


# ---------- 底层读取（保字节，不做 JSON 往返） ----------

def read_rows(path: str | os.PathLike) -> tuple[list[dict | None], list[str], list[int]]:
    """返回 (解析后的记录, 原始行文本, 1 基行号)。空行跳过但行号保持真实。"""
    rows: list[dict | None] = []
    raws: list[str] = []
    linenos: list[int] = []
    with io.open(path, encoding="utf-8", errors="replace") as f:
        for lineno, line in enumerate(f, 1):
            text = line.rstrip("\n").rstrip("\r")
            if not text.strip():
                continue
            linenos.append(lineno)
            raws.append(text)
            try:
                rows.append(json.loads(text))
            except Exception:
                rows.append(None)
    return rows, raws, linenos


def file_digest(path: str | os.PathLike) -> dict:
    h = hashlib.sha256()
    size = 0
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return {"sha256": h.hexdigest(), "size": size}


def rows_digest(raws: list[str]) -> str:
    h = hashlib.sha256()
    for line in raws:
        h.update(line.encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def _kind(row: dict | None) -> str | None:
    return None if not isinstance(row, dict) else str(row.get("record"))


def _self_hash(row: dict | None) -> str | None:
    if isinstance(row, dict) and row.get("self_hash"):
        return str(row["self_hash"])
    return None


# ---------- 统计 ----------

def scan(path: str | os.PathLike, kinds=DEFAULT_KINDS) -> dict:
    rows, raws, linenos = read_rows(path)
    kinds = tuple(kinds)
    by_kind: dict[str, int] = {}
    for row in rows:
        k = _kind(row) or "<unparseable>"
        by_kind[k] = by_kind.get(k, 0) + 1

    runs: list[dict] = []
    i = 0
    n = len(rows)
    while i < n:
        if _kind(rows[i]) in kinds:
            start = i
            while i < n and _kind(rows[i]) in kinds:
                i += 1
            runs.append({"start_index": start, "end_index": i - 1, "rows": i - start})
        else:
            i += 1

    removed = sum(r["rows"] for r in runs)
    ts_values = [r.get("ts") for r in rows if isinstance(r, dict) and r.get("ts")]
    d = file_digest(path)
    return {
        "path": str(path),
        "rows": len(rows),
        "bytes": d["size"],
        "sha256": d["sha256"],
        "sealed": sum(1 for r in rows if _self_hash(r)),
        "unsealed": sum(1 for r in rows if not _self_hash(r)),
        "by_kind": dict(sorted(by_kind.items(), key=lambda kv: -kv[1])),
        "archive_kinds": list(kinds),
        "archive_rows": removed,
        "business_rows": len(rows) - removed,
        "runs": runs,
        "ts_first": ts_values[0] if ts_values else None,
        "ts_last": ts_values[-1] if ts_values else None,
    }


# ---------- 归档 ----------

def archive(path: str | os.PathLike, out_dir: str | os.PathLike, *,
            kinds=DEFAULT_KINDS, dry_run: bool = False) -> dict:
    """把指定类型的行原样拷出去，并写清单。**不碰源文件。**"""
    import time
    kinds = tuple(kinds)
    rows, raws, linenos = read_rows(path)
    picked = [i for i, row in enumerate(rows) if _kind(row) in kinds]
    if not picked:
        return {"ok": False, "reason": "nothing_to_archive", "kinds": list(kinds)}

    stat = scan(path, kinds)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    label = "_".join(k.lower() for k in kinds)
    out = Path(out_dir)
    name = "%s-%s.jsonl" % (label, stamp)
    manifest_path = out / ("%s-%s.manifest.json" % (label, stamp))

    bridges = []
    for run in stat["runs"]:
        s, e = run["start_index"], run["end_index"]
        before = rows[s - 1] if s > 0 else None
        after = rows[e + 1] if e + 1 < len(rows) else None
        bridges.append({
            "start_index": s,
            "end_index": e,
            "rows": run["rows"],
            "prev_surviving_self_hash": _self_hash(before),
            "first_removed_self_hash": _self_hash(rows[s]),
            "last_removed_self_hash": _self_hash(rows[e]),
            "next_surviving_self_hash": _self_hash(after),
        })

    manifest = {
        "schema": ARCHIVE_SCHEMA,
        "created_at": stamp,
        "source": {"path": str(Path(path).resolve()), **file_digest(path),
                   "rows": len(rows), "sealed": stat["sealed"],
                   "ts_first": stat["ts_first"], "ts_last": stat["ts_last"]},
        "kinds": list(kinds),
        "archived_rows": len(picked),
        "remaining_rows": len(rows) - len(picked),
        "source_line_numbers": [linenos[i] for i in picked],
        "source_row_indices": picked,
        "runs": stat["runs"],
        "bridges": bridges,
    }

    if dry_run:
        return {"ok": True, "dry_run": True, "manifest": manifest,
                "archive_file": str(out / name)}

    out.mkdir(parents=True, exist_ok=True)
    payload = []
    for i in picked:
        payload.append(raws[i])
        payload.append("\n")
    data = "".join(payload).encode("utf-8")
    (out / name).write_bytes(data)

    manifest["archive"] = {
        "file": name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "rows": len(picked),
        "lines_sha256": rows_digest([raws[i] for i in picked]),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return {"ok": True, "archive_file": str(out / name),
            "manifest_file": str(manifest_path), "manifest": manifest}


# ---------- 校验 ----------

def verify(manifest_path: str | os.PathLike) -> dict:
    """证明"搬走的就是这些行，源文件这一段没被改过"。

    允许源文件**追加**（账本是只追加的），但已归档区域必须逐字节不变。
    """
    mf = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    src = mf["source"]["path"]
    problems: list[dict] = []

    arc_path = Path(manifest_path).parent / mf["archive"]["file"]
    if not arc_path.exists():
        return {"ok": False, "problems": [{"reason": "archive_file_missing"}]}
    arc_bytes = arc_path.read_bytes()
    if hashlib.sha256(arc_bytes).hexdigest() != mf["archive"]["sha256"]:
        problems.append({"reason": "archive_digest_mismatch"})
    arc_lines = [ln for ln in arc_bytes.decode("utf-8").split("\n") if ln.strip()]

    rows, raws, linenos = read_rows(src)
    indices = mf["source_row_indices"]
    if len(arc_lines) != len(indices):
        problems.append({"reason": "archive_row_count_mismatch",
                         "archive": len(arc_lines), "index": len(indices)})

    mismatches = 0
    first_mismatch = None
    for pos, (line, idx) in enumerate(zip(arc_lines, indices)):
        if idx >= len(raws) or raws[idx] != line:
            mismatches += 1
            if first_mismatch is None:
                first_mismatch = {"position": pos, "source_index": idx}
    if mismatches:
        problems.append({"reason": "source_region_changed", "mismatches": mismatches,
                         "first": first_mismatch})

    expected_rows = mf["source"]["rows"]
    appended = max(0, len(rows) - expected_rows)
    if len(rows) < expected_rows:
        problems.append({"reason": "source_shrank", "now": len(rows),
                         "expected": expected_rows})

    cur = file_digest(src)
    return {
        "ok": not problems,
        "problems": problems,
        "source_rows_now": len(rows),
        "source_rows_at_archive": expected_rows,
        "source_appended_since": appended,
        "source_sha256_now": cur["sha256"],
        "source_sha256_at_archive": mf["source"]["sha256"],
        "archive_rows": len(arc_lines),
        "kinds": mf["kinds"],
    }


# ---------- 压缩（另存一份，绝不覆盖源） ----------

def compact(path: str | os.PathLike, out_path: str | os.PathLike,
            manifest_path: str | os.PathLike, *, force: bool = False,
            allow_broken_source: bool = False) -> dict:
    """把归档类型从账本里去掉，**另存**成一个新文件，源文件一个字节都不动。

    源链若本来就断（2026-11-17 双中枢并发写入把链写分叉了，见 UAI-12），默认**拒绝**：
    在一条已经断了的链上再做一层变换，只会让"这条链到底可信不可信"更难说清。
    确实要压，就显式 allow_broken_source：既有断点会被写进 COMPACTION 一并声明，
    verify_compacted 才允许它们通过——**未声明的断点依然判红**。
    """
    import time
    from ledger_chain import self_hash as _sh, verify_chain as _verify_chain

    mf = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    out = Path(out_path)
    if out.exists() and not force:
        return {"ok": False, "reason": "output_exists", "out": str(out)}

    vr = verify(manifest_path)
    if not vr["ok"]:
        return {"ok": False, "reason": "archive_verify_failed", "detail": vr["problems"]}

    rows, raws, linenos = read_rows(path)
    source_chain = _verify_chain(path)
    if not source_chain["ok"] and not allow_broken_source:
        return {
            "ok": False,
            "reason": "source_chain_broken",
            "broken_count": len(source_chain["broken"]),
            "first": [{"lineno": ln, "reason": why}
                      for ln, why in source_chain["broken"][:6]],
            "hint": "源链已分叉（多为多写者并发追加）。要压请显式 --allow-broken-source，"
                    "断点会被写进 COMPACTION 一并声明。",
        }

    lineno_to_index = {ln: idx for idx, ln in enumerate(linenos)}
    break_reasons: dict[int, list[str]] = {}
    for ln, why in source_chain["broken"]:
        idx = lineno_to_index.get(ln)
        if idx is None:
            continue
        break_reasons.setdefault(idx, []).append(why)

    removed = set(mf["source_row_indices"])
    keep = [i for i in range(len(rows)) if i not in removed]
    bridges = mf["bridges"]
    head_before = next((_self_hash(rows[i]) for i in reversed(range(len(rows)))
                        if _self_hash(rows[i])), GENESIS)
    last_surviving = _self_hash(rows[keep[-1]]) if keep else GENESIS

    compaction = {
        "record": "COMPACTION",
        "schema": ARCHIVE_SCHEMA,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_path": mf["source"]["path"],
        "source_sha256": mf["source"]["sha256"],
        "source_rows": mf["source"]["rows"],
        "source_head_self_hash": head_before,
        "removed_rows": mf["archived_rows"],
        "removed_kinds": mf["kinds"],
        "archive_file": mf["archive"]["file"],
        "archive_sha256": mf["archive"]["sha256"],
        "remaining_rows": len(keep),
        "kept_source_indices": keep,
        "bridges": [{"start_index": b["start_index"], "end_index": b["end_index"],
                     "rows": b["rows"],
                     "prev_surviving_self_hash": b["prev_surviving_self_hash"],
                     "last_removed_self_hash": b["last_removed_self_hash"]}
                    for b in bridges],
        "declared_break_source_indices": sorted(break_reasons),
        "declared_breaks": [
            {"source_index": idx, "reasons": sorted(set(why)),
             "record_prev_hash": rows[idx].get("prev_hash"),
             "record_self_hash": rows[idx].get("self_hash")}
            for idx, why in sorted(break_reasons.items())
        ],
        "chain_note": ("不连续点只允许出现在 bridges 或 declared_breaks 里；"
                       "其余相邻记录必须首尾相接"),
    }
    compaction["prev_hash"] = last_surviving
    compaction["self_hash"] = _sh(compaction, last_surviving)

    payload = "\n".join(raws[i] for i in keep) + "\n"
    payload += json.dumps(compaction, ensure_ascii=False, sort_keys=True) + "\n"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(payload, encoding="utf-8")
    return {"ok": True, "out": str(out), "remaining_rows": len(keep),
            "removed_rows": mf["archived_rows"],
            "declared_breaks": len(compaction["declared_break_source_indices"]),
            "source_chain_ok": source_chain["ok"],
            "compaction": compaction}


def verify_compacted(compacted_path: str | os.PathLike,
                     manifest_path: str | os.PathLike) -> dict:
    """压缩版自身的完整性：逐条重算 self_hash + 每个不连续点都必须有出处。

    出处只有两种：**归档桥接**（这一段被搬走了）或 **源链本来就断**（已声明）。
    两者之外的不连续一律判红——包括"忘了声明"的情况。
    """
    mf = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    bridge_pairs = {(b["prev_surviving_self_hash"], b["last_removed_self_hash"])
                    for b in mf["bridges"]}

    from ledger_chain import self_hash as _sh

    rows, _, _ = read_rows(compacted_path)
    comp_rows = [r for r in rows if isinstance(r, dict) and r.get("record") == "COMPACTION"]
    if len(comp_rows) != 1:
        return {"ok": False, "rows": len(rows), "compaction_records": len(comp_rows),
                "problems": [{"reason": "compaction_record_count", "count": len(comp_rows)}]}

    comp = comp_rows[0]
    decl = comp.get("declared_breaks")
    if decl is None:
        decl = [{"source_index": i} for i in comp.get("declared_break_source_indices") or []]
    declared_indices = {d.get("source_index") for d in decl}
    kept = comp.get("kept_source_indices") or []

    problems: list[dict] = []
    prev = GENESIS
    bridged = declared_hits = 0
    body = [r for r in rows if isinstance(r, dict) and r.get("record") != "COMPACTION"]
    total = len(body)
    for pos, row in enumerate(body):
        src_index = kept[pos] if pos < len(kept) else None
        if not row.get("self_hash"):
            # 未链化的历史记录：与 ledger_chain 同一口径，不参与链路，也不算失败
            continue
        calc = _sh(row, row.get("prev_hash") or GENESIS)
        if calc != row.get("self_hash"):
            problems.append({"reason": "self_hash_mismatch", "index": pos,
                             "source_index": src_index})
        if row.get("prev_hash") != prev:
            if (prev, row.get("prev_hash")) in bridge_pairs:
                bridged += 1
            elif src_index in declared_indices:
                declared_hits += 1
            else:
                problems.append({"reason": "unbridged_discontinuity", "index": pos,
                                 "source_index": src_index, "prev": prev,
                                 "record_prev": row.get("prev_hash")})
        prev = row.get("self_hash")

    if comp.get("prev_hash") != prev:
        problems.append({"reason": "compaction_prev_mismatch"})
    if _sh(comp, comp.get("prev_hash") or GENESIS) != comp.get("self_hash"):
        problems.append({"reason": "compaction_self_hash_mismatch"})

    return {"ok": not problems, "problems": problems, "rows": total,
            "compaction_records": 1,
            "bridges_available": len(bridge_pairs), "bridges_used": bridged,
            "declared_breaks": len(declared_indices), "declared_hits": declared_hits,
            "source_had_declared_breaks": bool(declared_indices)}

# ---------- CLI ----------

def main(argv: list[str]) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="UAI-09 账本心跳归档（不改历史）")
    ap.add_argument("--ledger",
                    default=r"C:\Users\33389\.shared\comms-v2\cutover-base-20261109\data\ledger.jsonl")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS))
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("stats")
    a = sub.add_parser("archive")
    a.add_argument("--out", default=None)
    a.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("verify")
    v.add_argument("--manifest", required=True)
    c = sub.add_parser("compact")
    c.add_argument("--manifest", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--force", action="store_true")
    c.add_argument("--allow-broken-source", action="store_true")
    vc = sub.add_parser("verify-compacted")
    vc.add_argument("--manifest", required=True)
    vc.add_argument("--compacted", required=True)
    args = ap.parse_args(argv)

    kinds = tuple(k.strip() for k in args.kinds.split(",") if k.strip())
    if args.cmd == "stats":
        print(json.dumps(scan(args.ledger, kinds), ensure_ascii=False, indent=2))
        return 0
    if args.cmd == "archive":
        out = args.out or str(Path(args.ledger).parent / "archive")
        res = archive(args.ledger, out, kinds=kinds, dry_run=args.dry_run)
        print(json.dumps({k: v for k, v in res.items() if k != "manifest"},
                         ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    if args.cmd == "verify":
        res = verify(args.manifest)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res["ok"] else 1
    if args.cmd == "compact":
        res = compact(args.ledger, args.out, args.manifest, force=args.force,
                      allow_broken_source=args.allow_broken_source)
        print(json.dumps({k: v for k, v in res.items() if k != "compaction"},
                         ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1
    res = verify_compacted(args.compacted, args.manifest)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
