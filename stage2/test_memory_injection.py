"""E-MI: memory auto-injection integration test."""
import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

PASSES, FAILS = [], []

def check(name, cond, detail=''):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    # 1. inbound_bridge.py has inject_memory parameter
    ib = Path(r'C:\Users\33389\workspace\projects\universal-agent-interface\stage2\inbound_bridge.py').read_text(encoding='utf-8')
    check("MI-1 has inject_memory param", "inject_memory" in ib, "")
    check("MI-2 has inject_context call", "inject_context" in ib, "")
    check("MI-3 injection failure silent", "except Exception" in ib, "")

    # 2. Real memory injection works
    from team_memory import TeamMemoryBridge
    mem = TeamMemoryBridge()
    injected = mem.inject_context('codex 是实现主力')
    check("MI-4 inject works", len(injected) > 20, injected[:80])
    mem.close()

    # 3. Original task text preserved at the end
    check("MI-5 original task preserved", "codex 是实现主力" in injected, "")

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())