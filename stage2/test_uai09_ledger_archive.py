"""UAI-09 自检：账本心跳归档（不改历史、逐字节可验证、压缩可桥接验证）。"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
sys.path.insert(0, str(STAGE2))

import ledger_archive as LA          # noqa: E402
import ledger_chain as LC            # noqa: E402

PASSES, FAILS = [], []
HB = "INTERFACE_HEARTBEAT"


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-56s %s" % ("PASS" if cond else "FAIL", name, detail))


def make_ledger(path: Path, hb_runs=(40, 120, 25), biz_between=5):
    """造一条带真实哈希链的账本：心跳块与业务记录交替。"""
    rows = []
    prev = LC.GENESIS
    seq = 0

    def add(rec):
        nonlocal prev, seq
        rec = dict(rec)
        rec.setdefault("seq", seq)
        sealed = LC.seal(rec, prev)
        prev = sealed["self_hash"]
        seq += 1
        rows.append(sealed)

    for gi, count in enumerate(hb_runs):
        add({"record": "HUB_START", "ts": "2026-11-17T00:00:%02dZ" % gi})
        for k in range(count):
            add({"record": HB, "interface_id": "dsh", "ts": "2026-11-17T00:01:%02dZ" % (k % 60)})
        for k in range(biz_between):
            add({"record": "TASK_STATE", "task_id": "t-%d-%d" % (gi, k), "ts": "2026-11-17T00:02:00Z"})
    add({"record": "VERDICT", "verdict": "通过", "ts": "2026-11-17T00:03:00Z"})

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
    return path



def broken_source_roundtrip(tmp: Path) -> None:
    """源链本来就断（多写者并发导致分叉）时：默认拒绝，显式允许则必须声明断点。"""
    src = make_ledger(tmp / "broken" / "ledger.jsonl", hb_runs=(10, 10), biz_between=3)
    head = LC.chain_head(src)
    # 造一个叉：两条记录用同一个 prev（两个写者各自以为自己是下一个）
    for tag in ("fork-a", "fork-b"):
        rec = {"record": "TASK_ACCEPTED", "task_id": tag, "ts": "2026-11-17T10:00:00Z"}
        with src.open("a", encoding="utf-8") as f:
            f.write(json.dumps(LC.seal(rec, head), ensure_ascii=False, sort_keys=True) + "\n")

    broken = LC.verify_chain(src)
    check("UAI-09 造出的分叉确实被链校验判红", broken["ok"] is False,
          "broken=%d" % len(broken["broken"]))

    arc_dir = tmp / "broken" / "archive"
    a = LA.archive(src, arc_dir, kinds=(HB,))
    mf = a["manifest_file"]
    out = tmp / "broken" / "ledger.compacted.jsonl"

    r1 = LA.compact(src, out, mf)
    check("UAI-09 源链断 → 默认拒绝压缩", r1["ok"] is False
          and r1["reason"] == "source_chain_broken", str(r1.get("reason")))
    check("UAI-09 拒绝时给出断点样本与提示",
          bool(r1.get("first")) and "allow-broken-source" in (r1.get("hint") or ""), "")
    check("UAI-09 拒绝时不落文件", not out.exists(), "")

    r2 = LA.compact(src, out, mf, allow_broken_source=True)
    check("UAI-09 显式允许后压缩成功并声明断点",
          r2["ok"] is True and r2["declared_breaks"] >= 1,
          "declared=%s" % r2.get("declared_breaks"))

    vc = LA.verify_compacted(out, mf)
    check("UAI-09 压缩版把已声明断点放行", vc["ok"] is True,
          json.dumps(vc["problems"], ensure_ascii=False))
    check("UAI-09 已声明断点有命中计数", vc["declared_hits"] >= 1, str(vc["declared_hits"]))

    # 抹掉声明 → 同一处不连续必须判红（证明"未声明的不连续"跑不掉）
    rows = [json.loads(l) for l in out.read_text(encoding="utf-8").split("\n") if l.strip()]
    rows[-1]["declared_break_source_indices"] = []
    rows[-1]["declared_breaks"] = []
    rows[-1].pop("self_hash", None)
    rows[-1]["self_hash"] = LC.self_hash(rows[-1], rows[-1]["prev_hash"])
    stripped = "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n"
    out.write_text(stripped, encoding="utf-8")
    vc2 = LA.verify_compacted(out, mf)
    check("UAI-09 抹掉断点声明 → 判红", vc2["ok"] is False,
          json.dumps([p["reason"] for p in vc2["problems"]]))

def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="uai09-"))
    src = make_ledger(tmp / "src" / "ledger.jsonl")
    arc_dir = tmp / "src" / "archive"

    # ---------- 统计 ----------
    st = LA.scan(src, kinds=(HB,))
    check("UAI-09 统计出心跳行数", st["archive_rows"] == 40 + 120 + 25, str(st["archive_rows"]))
    check("UAI-09 统计出业务行数", st["business_rows"] == st["rows"] - st["archive_rows"],
          "%d/%d" % (st["business_rows"], st["rows"]))
    check("UAI-09 统计出心跳连续块", len(st["runs"]) == 3,
          json.dumps([r["rows"] for r in st["runs"]]))
    check("UAI-09 统计全部已封链", st["unsealed"] == 0, str(st["unsealed"]))

    # ---------- 归档（不改源） ----------
    src_before = LA.file_digest(src)
    res = LA.archive(src, arc_dir, kinds=(HB,))
    check("UAI-09 归档成功", res["ok"] is True, str(res.get("archive_file")))
    check("UAI-09 源文件未被修改", LA.file_digest(src) == src_before, "")

    mf = json.loads(Path(res["manifest_file"]).read_text(encoding="utf-8"))
    arc_path = Path(res["archive_file"])
    check("UAI-09 清单记录了源文件指纹",
          mf["source"]["sha256"] == src_before["sha256"] and mf["source"]["rows"] == st["rows"], "")
    check("UAI-09 清单记录了归档字节指纹",
          mf["archive"]["sha256"] == LA.file_digest(arc_path)["sha256"], "")

    # 逐字节相等（不做 JSON 往返）
    src_rows, src_raws, _ = LA.read_rows(src)
    arc_lines = [ln for ln in arc_path.read_text(encoding="utf-8").split("\n") if ln.strip()]
    identical = all(arc_lines[pos] == src_raws[idx]
                    for pos, idx in enumerate(mf["source_row_indices"]))
    check("UAI-09 归档内容与源行逐字节相同", identical, "%d 行" % len(arc_lines))
    check("UAI-09 归档只包含心跳行",
          all(json.loads(l)["record"] == HB for l in arc_lines), "")

    # 桥接信息
    check("UAI-09 每段都记下了桥接 self_hash",
          len(mf["bridges"]) == len(st["runs"])
          and all(b["last_removed_self_hash"] for b in mf["bridges"]), "")

    # ---------- 校验 ----------
    vr = LA.verify(res["manifest_file"])
    check("UAI-09 校验通过", vr["ok"] is True, json.dumps(vr["problems"], ensure_ascii=False))
    check("UAI-09 校验报告追加行数", vr["source_appended_since"] == 0, str(vr["source_appended_since"]))

    # 篡改已归档区域 → 必须被抓到
    keep = src.read_text(encoding="utf-8")
    bad = keep.replace('"interface_id": "dsh"', '"interface_id": "codex"', 1)
    src.write_text(bad, encoding="utf-8")
    vr2 = LA.verify(res["manifest_file"])
    reasons = [p["reason"] for p in vr2["problems"]]
    check("UAI-09 源行被改 → 校验判红", vr2["ok"] is False
          and "source_region_changed" in reasons, json.dumps(reasons))
    src.write_text(keep, encoding="utf-8")
    check("UAI-09 还原后校验恢复", LA.verify(res["manifest_file"])["ok"] is True, "")

    # 追加新行是允许的（只追加账本）
    extra = LC.seal({"record": "TASK_ACCEPTED", "task_id": "t-new", "ts": "2026-11-17T09:00:00Z"},
                    LC.chain_head(src))
    with src.open("a", encoding="utf-8") as f:
        f.write(json.dumps(extra, ensure_ascii=False, sort_keys=True) + "\n")
    vr3 = LA.verify(res["manifest_file"])
    check("UAI-09 源文件追加新行不判红，且报告追加数",
          vr3["ok"] is True and vr3["source_appended_since"] == 1,
          "appended=%s" % vr3["source_appended_since"])

    # ---------- 压缩（另存一份） ----------
    out = tmp / "src" / "ledger.compacted.jsonl"
    c = LA.compact(src, out, res["manifest_file"])
    check("UAI-09 压缩生成新文件", c["ok"] is True and out.exists(), str(c.get("out")))
    check("UAI-09 压缩不动源文件", LA.file_digest(src)["sha256"] == vr3["source_sha256_now"], "")

    again = LA.compact(src, out, res["manifest_file"])
    check("UAI-09 拒绝覆盖已存在的压缩文件", again["ok"] is False
          and again["reason"] == "output_exists", str(again.get("reason")))

    crows, _, _ = LA.read_rows(out)
    kinds = [r.get("record") for r in crows if isinstance(r, dict)]
    check("UAI-09 压缩版里没有心跳行", HB not in kinds, str(sorted(set(kinds))))
    check("UAI-09 压缩版剩下 业务行 + 1 条 COMPACTION",
          kinds.count("COMPACTION") == 1
          and len(kinds) - 1 == st["business_rows"] + 1,
          "rows=%d biz=%d" % (len(kinds), st["business_rows"]))

    comp = crows[-1]
    check("UAI-09 COMPACTION 绑定源文件指纹与归档指纹",
          comp["source_sha256"] == mf["source"]["sha256"]
          and comp["archive_sha256"] == mf["archive"]["sha256"], "")
    check("UAI-09 COMPACTION 记下桥接数", len(comp["bridges"]) == len(mf["bridges"]),
          str(len(comp["bridges"])))

    # ---------- 压缩版校验 ----------
    vc = LA.verify_compacted(out, res["manifest_file"])
    check("UAI-09 压缩版链可验证", vc["ok"] is True,
          json.dumps(vc["problems"], ensure_ascii=False))
    check("UAI-09 不连续点全部走桥接", vc["bridges_used"] >= 1
          and vc["bridges_used"] <= vc["bridges_available"],
          "used=%d/%d" % (vc["bridges_used"], vc["bridges_available"]))

    # 篡改压缩版里一条存活记录
    keep2 = out.read_text(encoding="utf-8")
    out.write_text(keep2.replace('"verdict": "\\u901a\\u8fc7"', '"verdict": "xx"', 1)
                   .replace('"record": "TASK_STATE"', '"record": "TASK_STATE_X"', 1),
                   encoding="utf-8")
    vc2 = LA.verify_compacted(out, res["manifest_file"])
    check("UAI-09 压缩版被篡改 → 判红", vc2["ok"] is False,
          json.dumps([p["reason"] for p in vc2["problems"]]))
    out.write_text(keep2, encoding="utf-8")

    # 去掉 COMPACTION 记录 → 判红
    kept = [ln for ln in keep2.split("\n") if ln.strip()]
    no_comp = "\n".join(kept[:-1]) + "\n"
    out.write_text(no_comp, encoding="utf-8")
    vc3 = LA.verify_compacted(out, res["manifest_file"])
    check("UAI-09 缺 COMPACTION 记录 → 判红", vc3["ok"] is False
          and any(p["reason"] == "compaction_record_count" for p in vc3["problems"]),
          json.dumps([p["reason"] for p in vc3["problems"]]))
    out.write_text(keep2, encoding="utf-8")

    # ---------- dry-run 不落盘 ----------
    dry = LA.archive(src, tmp / "dry", kinds=(HB,), dry_run=True)
    check("UAI-09 dry-run 不写文件", dry["ok"] is True and not (tmp / "dry").exists(),
          str((tmp / "dry").exists()))

    broken_source_roundtrip(tmp)

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
