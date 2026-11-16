"""E-H3L: three-layer verdict integrated into hub (I-4).

Exercises the real Hub._run_three_layer code path with real result artifacts,
no NATS required (Hub is constructed directly, method called directly).
"""
import json
import sys
import tempfile
from pathlib import Path

STAGE2 = Path(__file__).resolve().parent
PROTO = STAGE2.parent / "prototype"
sys.path.insert(0, str(STAGE2))
sys.path.insert(0, str(PROTO))

from hub import Hub, Task  # noqa: E402

PASSES, FAILS = [], []


def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))


def make_hub():
    base = Path(tempfile.mkdtemp())
    (base / "data").mkdir(parents=True, exist_ok=True)
    (base / "logs").mkdir(parents=True, exist_ok=True)
    return Hub(base, "nats://127.0.0.1:43321"), base


def make_task(hub_base, contract, implementer="codex", result_obj=None,
              result_text=None):
    """Create a Task with a real result artifact on disk."""
    task_id = "t-" + str(abs(hash((contract is not None, implementer, str(result_obj))))) [0:8]
    results_dir = hub_base / "data" / "results" / task_id
    results_dir.mkdir(parents=True, exist_ok=True)
    if result_obj is not None:
        p = results_dir / "result.json"
        p.write_text(json.dumps(result_obj, ensure_ascii=False), encoding="utf-8")
        media = "application/json"
    elif result_text is not None:
        p = results_dir / "result.txt"
        p.write_text(result_text, encoding="utf-8")
        media = "text/plain"
    else:
        p = None
        media = None

    pointer = None
    if p is not None:
        from core import file_uri_to_path  # noqa: F401  (proves import path works)
        pointer = {
            "uri": p.as_uri(),
            "sha256": __import__("hashlib").sha256(p.read_bytes()).hexdigest(),
            "bytes": p.stat().st_size,
            "media_type": media,
        }

    envelope = {
        "task_id": task_id,
        "trace_id": "tr-" + task_id,
        "idempotency_key": "idem-" + task_id,
        "created_by": "test",
        "instruction": "test instruction",
        "params": {"output_contract": contract} if contract is not None else {},
    }
    task = Task(
        task_id=task_id,
        envelope=envelope,
        trace_id=envelope["trace_id"],
        idempotency_key=envelope["idempotency_key"],
        state="SUCCEEDED",
    )
    task.selected_interface = implementer
    task.result_pointer = pointer
    return task


def main():
    hub, base = make_hub()

    # --- 1. 无 contract：不强制，返回 enforced=False ---
    t = make_task(base, None, result_obj={"ok": True})
    r = hub._run_three_layer(t, {"verdict": "通过", "checker": "tianshu"})
    check("H3L-1 no contract -> not enforced", r["enforced"] is False, str(r.get("enforced")))
    check("H3L-2 no contract -> no reject", r["reject"] is None, str(r.get("reject")))

    # --- 2. 有 contract 且全部通过 ---
    contract = {"forbidden": ["secret"], "invariants": [], "consistency_threshold": 0.8}
    t2 = make_task(base, contract, implementer="codex",
                   result_obj={"status": "done", "count": 3})
    r2 = hub._run_three_layer(t2, {"verdict": "通过", "checker": "tianshu"})
    check("H3L-3 contract -> enforced", r2["enforced"] is True, "")
    check("H3L-4 all pass -> no reject", r2["reject"] is None, str(r2.get("reject")))
    check("H3L-5 result has 3 layers",
          all(k in (r2["result"] or {}) for k in ("layer1", "layer2", "layer3")),
          str(sorted((r2["result"] or {}).keys())))

    # --- 3. L1 失败：产物含 forbidden 内容 -> 拒绝 ---
    t3 = make_task(base, contract, implementer="codex",
                   result_obj={"status": "done", "secret": "leaked-token"})
    r3 = hub._run_three_layer(t3, {"verdict": "通过", "checker": "tianshu"})
    check("H3L-6 forbidden content -> reject",
          r3["reject"] == "layer1_deterministic_failed", str(r3.get("reject")))

    # --- 4. L3 失败：核验者==实现者 -> 拒绝 ---
    t4 = make_task(base, contract, implementer="codex",
                   result_obj={"status": "done"})
    r4 = hub._run_three_layer(t4, {"verdict": "通过", "checker": "codex"})
    check("H3L-7 verifier==implementer -> reject",
          r4["reject"] == "verifier_is_implementer", str(r4.get("reject")))

    # --- 5. L3 失败：非 canonical 裁定词 -> 拒绝 ---
    t5 = make_task(base, contract, implementer="codex",
                   result_obj={"status": "done"})
    r5 = hub._run_three_layer(t5, {"verdict": "驳回", "checker": "tianshu"})
    check("H3L-8 non-canonical verdict -> reject",
          r5["reject"] == "verdict_not_canonical", str(r5.get("reject")))

    # --- 6. L2 失败：run_outputs 不一致 -> 拒绝 ---
    t6 = make_task(base, contract, implementer="codex",
                   result_obj={"status": "done"})
    r6 = hub._run_three_layer(t6, {
        "verdict": "通过", "checker": "tianshu",
        "run_outputs": [{"a": 1}, {"b": 2}, {"c": 3}],
    })
    check("H3L-9 inconsistent runs -> reject",
          r6["reject"] == "layer2_consistency_below_threshold", str(r6.get("reject")))

    # --- 7. 真实产物读取：文本产物也要能验 ---
    t7 = make_task(base, contract, implementer="codex",
                   result_text="all good, nothing leaked")
    r7 = hub._run_three_layer(t7, {"verdict": "通过", "checker": "tianshu"})
    check("H3L-10 text artifact validated", r7["reject"] is None, str(r7.get("reject")))

    # --- 8. 产物缺失：L1 仍能给出结论（空 output 不触发 forbidden）---
    t8 = make_task(base, contract, implementer="codex", result_obj=None)
    r8 = hub._run_three_layer(t8, {"verdict": "通过", "checker": "tianshu"})
    check("H3L-11 missing artifact -> no crash", isinstance(r8.get("result"), dict),
          str(type(r8.get("result")).__name__))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
