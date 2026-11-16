"""E-3LV: three-layer verdict test."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from three_layer_verdict import ThreeLayerVerdict

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    envelope = {
        "output_contract": {
            "invariants": ["echo hello"],
            "forbidden": ["password", "secret"],
            "consistency_threshold": 0.8,
        }
    }
    v = ThreeLayerVerdict(envelope)

    # Layer 1: deterministic
    output = {"result": "success", "file": "output.txt"}
    artifacts = {}
    l1 = v.layer1_deterministic(output, artifacts)
    check("3LV-L1 echo invariant passes", l1["pass"] is True, str(l1))

    # Layer 1: forbidden content detected
    output_bad = {"result": "success", "password": "abc123"}
    l1b = v.layer1_deterministic(output_bad, artifacts)
    check("3LV-L1 forbidden content detected", l1b["pass"] is False, str(l1b))

    # Layer 2: consistency - all same
    outputs_same = [{"r": "ok"}, {"r": "ok"}, {"r": "ok"}]
    l2 = v.layer2_consistency(outputs_same)
    check("3LV-L2 3 identical runs = 1.0", l2["consistency"] == 1.0, str(l2))

    # Layer 2: consistency - all different
    outputs_diff = [{"r": "a"}, {"r": "b"}, {"r": "c"}]
    l2b = v.layer2_consistency(outputs_diff)
    check("3LV-L2 all different < threshold", l2b["consistency"] < 0.8, str(l2b))

    # Layer 3: independent verdict
    l3 = v.layer3_independent("通过", "verifier-1", "implementer-1")
    check("3LV-L3 independent verifier", l3["pass"] is True, str(l3))

    l3b = v.layer3_independent("通过", "implementer-1", "implementer-1")
    check("3LV-L3 same agent = not independent", l3b["pass"] is False, str(l3b))

    l3c = v.layer3_independent("驳回", "verifier-1", "implementer-1")
    check("3LV-L3 non-canonical verdict", l3c["pass"] is False, str(l3c))

    # execute_all
    all_ok = v.execute_all(output, artifacts, outputs_same, "通过", "verifier-1", "implementer-1")
    check("3LV-execute_all overall_pass", all_ok["overall_pass"] is True, str(all_ok.get("overall_pass")))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
