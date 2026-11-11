"""四档基线对照基准（E-03）。

用法：
    python run_benchmark.py --self-check        # CI 用：跑一遍并断言档位单调改善
    python run_benchmark.py --tasks 2000 --seed 20261112 --out RESULTS.json

判定「更好」的口径（按重要性排序）：
    1. wrong_accepted（错误被当成成功）越低越好 —— 这是"伪证"问题
    2. lost（任务丢失）越低越好
    3. stuck（无终态）越低越好
    4. duplicate_exec（重复执行）越低越好
    5. ops_per_task（开销）作为代价项，允许上升
"""

from __future__ import annotations

import argparse
import json
import sys

import sim

# 每档的最低要求（self-check 用）
EXPECTATIONS = {
    "T0": {"wrong_accepted_min": 1},          # 无防护必然出现错误收口
    "T1": {"lost_max": 0},                    # 幂等重投消掉丢失
    "T2": {"stuck_max": 0},                   # 租约回收消掉卡死
    "T3": {"wrong_accepted_max": 0},          # 证据+裁定消掉错误收口
}


def fmt(rows: list[dict]) -> str:
    head = ("tier", "tasks", "lost", "duplicate", "wrong", "stuck", "ok", "ops/task")
    line = "%-5s %6s %6s %10s %6s %6s %6s %9s" % head
    out = [line, "-" * len(line)]
    for r in rows:
        out.append("%-5s %6d %6d %10d %6d %6d %6d %9.3f" % (
            r["tier"], r["tasks"], r["lost"], r["duplicate_exec"],
            r["wrong_accepted"], r["stuck"], r["recovered_ok"], r["ops_per_task"]))
    return "\n".join(out)


def self_check(rows: list[dict]) -> list[str]:
    errs = []
    by = {r["tier"]: r for r in rows}

    if by["T0"]["wrong_accepted"] < EXPECTATIONS["T0"]["wrong_accepted_min"]:
        errs.append("T0 应至少出现 1 次错误收口，实测 %d" % by["T0"]["wrong_accepted"])
    if by["T1"]["lost"] > EXPECTATIONS["T1"]["lost_max"]:
        errs.append("T1 应消掉任务丢失，实测 lost=%d" % by["T1"]["lost"])
    if by["T2"]["stuck"] > EXPECTATIONS["T2"]["stuck_max"]:
        errs.append("T2 应消掉卡死，实测 stuck=%d" % by["T2"]["stuck"])
    if by["T3"]["wrong_accepted"] > EXPECTATIONS["T3"]["wrong_accepted_max"]:
        errs.append("T3 应消掉错误收口，实测 wrong=%d" % by["T3"]["wrong_accepted"])

    # 各档只对自己的目标故障负责（实测结论：幂等不消伪证）
    if by["T1"]["wrong_accepted"] <= by["T0"]["wrong_accepted"]:
        # 允许上升：T1 救活了本会丢失的任务，暴露面变大
        pass
    if by["T2"]["wrong_accepted"] >= by["T1"]["wrong_accepted"]:
        errs.append("T2（围栏）应降低错误收口，实测 %d vs T1 %d"
                    % (by["T2"]["wrong_accepted"], by["T1"]["wrong_accepted"]))
    if by["T3"]["wrong_accepted"] != EXPECTATIONS["T3"]["wrong_accepted_max"]:
        errs.append("T3 错误收口应为 0，实测 %d" % by["T3"]["wrong_accepted"])

    # 丢失与卡死必须单调不增
    for prev, cur in (("T0", "T1"), ("T1", "T2"), ("T2", "T3")):
        if by[cur]["lost"] > by[prev]["lost"]:
            errs.append("%s→%s 丢失变差" % (prev, cur))
        if by[cur]["stuck"] > by[prev]["stuck"]:
            errs.append("%s→%s 卡死变差" % (prev, cur))
        if by[cur]["duplicate_exec"] > by[prev]["duplicate_exec"]:
            errs.append("%s→%s 重复执行变差" % (prev, cur))

    # 代价项：T3 的开销应高于 T0（防护不是免费的）
    if by["T3"]["ops_per_task"] <= by["T0"]["ops_per_task"]:
        errs.append("T3 开销应高于 T0（防护有成本），实测 %.3f vs %.3f"
                    % (by["T3"]["ops_per_task"], by["T0"]["ops_per_task"]))

    return errs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", type=int, default=2000)
    ap.add_argument("--seed", type=int, default=20261112)
    ap.add_argument("--out", default=None)
    ap.add_argument("--self-check", action="store_true")
    args = ap.parse_args()

    stats = sim.run_all(args.tasks, args.seed)
    rows = [s.as_row() for s in stats]

    print("四档基线对照（仿真；故障注入 %s）" % json.dumps(sim.FAULTS, ensure_ascii=False))
    print("tasks=%d seed=%d\n" % (args.tasks, args.seed))
    print(fmt(rows))

    errs = self_check(rows)
    print()
    if errs:
        print("SELF-CHECK FAIL（%d 项）" % len(errs))
        for e in errs:
            print("  ! " + e)
    else:
        print("SELF-CHECK PASS：四档单调改善，且 T3 的 wrong_accepted=0")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump({"faults": sim.FAULTS, "tasks": args.tasks, "seed": args.seed,
                       "rows": rows, "self_check_errors": errs}, f,
                      ensure_ascii=False, indent=2)
        print("\n写入 %s" % args.out)

    if args.self_check and errs:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
