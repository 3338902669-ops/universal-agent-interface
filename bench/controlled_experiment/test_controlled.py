"""E-02 自检：受控对照实验包。

关键断言：
  1. 判分器是纯函数（同输入同输出）；
  2. 空 raw 目录 → analyze 非 0 退出并报 no_data（不许 0 失败 → 100% 通过）；
  3. 通道失败与能力失败分开统计；
  4. 两档 fixture/判分器不同 → compare 判为不可比。
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import analyze  # noqa: E402
import run as R  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def write_run(d: Path, rows: list[dict], fixture="f" * 64, scoring="s" * 64):
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps({
        "seat": "t", "arm": "with_skill", "fixture_sha256": fixture,
        "scoring_frozen_sha256": scoring}), encoding="utf-8")
    with (d / "attempts.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def main() -> int:
    # ---------- 1 判分器纯函数 ----------
    t = {"scoring": {"kind": "exact", "expect": "PONG"}}
    a1 = R.score(t, "  PONG \n")
    a2 = R.score(t, "PONG")
    check("E-02 exact 判分去空白后相等", a1[0] is True, str(a1))
    check("E-02 判分器纯函数（同输入同输出）", a1 == a2, "%s vs %s" % (a1, a2))
    check("E-02 exact 不通过分支", R.score(t, "PON")[0] is False, "")

    t2 = {"scoring": {"kind": "numeric", "expect": 391, "tolerance": 0}}
    check("E-02 numeric 正确", R.score(t2, "391")[0] is True, "")
    check("E-02 numeric 容差外不通过", R.score(t2, "392")[0] is False, "")
    check("E-02 numeric 不可解析不通过", R.score(t2, "三十九")[0] is False, "")

    t3 = {"scoring": {"kind": "regex", "pattern": "^[a-z]{3}[0-9]{2}$"}}
    check("E-02 regex 命中", R.score(t3, "abc12")[0] is True, "")
    check("E-02 regex 不命中", R.score(t3, "abc1")[0] is False, "")

    check("E-02 未知判分类型返回 False + 原因",
          R.score({"scoring": {"kind": "bogus"}}, "x")[0] is False, "")

    # ---------- 2 题目夹具 ----------
    tasks = R.load_tasks()
    check("E-02 夹具 6 道题", len(tasks) == 6, str(len(tasks)))
    check("E-02 每题都有 scoring 与 id",
          all("scoring" in x and "id" in x for x in tasks), "")
    check("E-02 题目标题唯一", len({x["id"] for x in tasks}) == len(tasks), "")

    # ---------- 3 空目录 → 非 0 ----------
    empty = Path(tempfile.mkdtemp(prefix="ce-empty-"))
    rc = analyze.main(["--raw", str(empty)])
    check("E-02 空 raw → analyze 非 0 退出", rc == 4, str(rc))
    s = analyze.summarize(empty)
    check("E-02 空 raw → verdict=no_data", s["verdict"] == "no_data", s["verdict"])

    # ---------- 4 分类统计 ----------
    d = Path(tempfile.mkdtemp(prefix="ce-run-"))
    write_run(d, [
        {"task_id": "T01", "outcome": "pass", "duration_s": 2.0},
        {"task_id": "T02", "outcome": "capability_failure", "duration_s": 3.0},
        {"task_id": "T03", "outcome": "channel_failure", "duration_s": 1.0},
        {"task_id": "T04", "outcome": "pass", "duration_s": 2.0},
    ])
    s = analyze.summarize(d)
    check("E-02 通过/能力失败/通道失败分开计数",
          (s["pass"], s["capability_failure"], s["channel_failure"]) == (2, 1, 1), str(s))
    check("E-02 通过率按扣除通道失败后计算", s["pass_rate"] == round(2 / 3, 4), str(s["pass_rate"]))
    check("E-02 通道失败率 = 1/4", s["channel_failure_rate"] == 0.25, str(s["channel_failure_rate"]))
    check("E-02 通道噪声超 20% → verdict=invalid_channel_noise",
          s["verdict"] == "invalid_channel_noise", s["verdict"])

    # ---------- 5 可比性 ----------
    d2 = Path(tempfile.mkdtemp(prefix="ce-run2-"))
    write_run(d2, [
        {"task_id": "T01", "outcome": "pass", "duration_s": 2.0},
        {"task_id": "T02", "outcome": "pass", "duration_s": 2.0},
        {"task_id": "T03", "outcome": "pass", "duration_s": 2.0},
        {"task_id": "T04", "outcome": "pass", "duration_s": 2.0},
    ])
    s2 = analyze.summarize(d2)
    check("E-02 无通道噪声 → verdict=valid", s2["verdict"] == "valid", s2["verdict"])
    cmp_same = analyze.compare(s, s2)
    check("E-02 同 fixture/判分器但一档噪声 → 不可比",
          cmp_same["comparable"] is False and "通道噪声" in cmp_same["note"], cmp_same["note"])

    d3 = Path(tempfile.mkdtemp(prefix="ce-run3-"))
    write_run(d3, [{"task_id": "T01", "outcome": "pass", "duration_s": 1.0}],
              fixture="x" * 64)
    s3 = analyze.summarize(d3)
    cmp_diff = analyze.compare(s2, s3)
    check("E-02 fixture 不同 → 不可比",
          cmp_diff["comparable"] is False and cmp_diff["same_fixture"] is False, cmp_diff["note"])

    # ---------- 6 analyze.compare 空数据 ----------
    rc = analyze.main(["--compare", str(empty), str(d2)])
    check("E-02 compare 遇空数据非 0 退出", rc == 3, str(rc))

    # ---------- 7 冻结清单 ----------
    import manifest as MF
    cur = MF.build()
    check("E-02 清单四件齐全", all("sha256" in cur["files"][r] for r in MF.PINNED), str(len(MF.PINNED)))
    check("E-02 清单题数=夹具题数", cur["tasks"] == len(R.load_tasks()), "%s" % cur["tasks"])

    man_path = HERE / "MANIFEST.json"
    if man_path.exists():
        rc_ok = MF.main(["--verify", str(man_path)])
        check("E-02 已提交清单与当前文件一致", rc_ok == 0, "rc=%s" % rc_ok)
        other = Path(tempfile.mkdtemp(prefix="ce-man-")) / "m.json"
        stale = json.loads(man_path.read_text(encoding="utf-8-sig"))
        stale["files"]["run.py"] = {"sha256": "0" * 64, "bytes": 1}
        other.write_text(json.dumps(stale), encoding="utf-8")
        rc_bad = MF.main(["--verify", str(other)])
        check("E-02 清单被改 → 拒跑(非 0)", rc_bad == 2, "rc=%s" % rc_bad)
    else:
        check("E-02 已提交清单存在", False, "MANIFEST.json missing")

    print("\nRESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
