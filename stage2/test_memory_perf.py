"""E-MP: memory injection performance test (cache + circuit breaker)."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from team_memory import TeamMemoryBridge

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    mem = TeamMemoryBridge()
    q = "codex 是实现主力"

    t0 = time.time(); mem.inject_context(q); cold = time.time() - t0
    t1 = time.time(); mem.inject_context(q); warm = time.time() - t1

    check("MP-1 cold call completes", cold < 10, "%.3fs" % cold)
    check("MP-2 cached call < 50ms", warm < 0.05, "%.4fs" % warm)
    check("MP-3 cache is faster than cold", warm < cold / 10, "cold=%.3f warm=%.4f" % (cold, warm))
    mem.close()

    # circuit breaker: unreachable Neo4j must fail fast then short-circuit
    bad = TeamMemoryBridge(uri="bolt://127.0.0.1:59999")
    t2 = time.time(); r1 = bad.inject_context("test task"); first = time.time() - t2
    t3 = time.time(); r2 = bad.inject_context("test task"); second = time.time() - t3
    bad.close()

    check("MP-4 unreachable fails fast", first < 5, "%.3fs" % first)
    check("MP-5 returns original text on failure", r1 == "test task", repr(r1[:40]))
    check("MP-6 circuit opens (2nd call instant)", second < 0.01, "%.5fs" % second)
    check("MP-7 circuit returns original text", r2 == "test task", repr(r2[:40]))

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
