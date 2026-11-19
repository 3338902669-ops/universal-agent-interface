"""E-RT: SmartRouter real data integration test."""
import sys, json, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from smart_router import SmartRouter, AgentProfile

PASSES, FAILS = [], []

def check(name, cond, detail=''):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    ledger = r"C:\Users\33389\workspace\projects\universal-agent-interface\prototype\data\ledger.jsonl"
    work = Path(tempfile.mkdtemp())
    router = SmartRouter(work / 'router.json')

    router.register(AgentProfile("codex", ["code", "deploy", "test", "frontend", "backend"], "impl"))
    router.register(AgentProfile("dsh", ["deploy", "security", "audit", "docs"], "guard"))

    result = router.load_stats_from_ledger(ledger)
    check("RT-1 real ledger load", result["loaded"] > 0, str(result))

    codex = router.agents.get("codex")
    if codex:
        check("RT-2 codex stats has data", len(codex.stats) > 0, str(codex.stats))
        d = router.route("fix login page CSS")
        has_hist = "historical" in d.get("reason", "").lower() or "history" in d.get("reason", "").lower()
        check("RT-3 route uses history", has_hist, d.get("reason", "")[:100])

    router._save()
    saved = json.loads((work / 'router.json').read_text())
    check("RT-4 persist stats", any(a.get("stats") for a in saved.get("agents", [])), "")

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    import tempfile
    sys.exit(main())