"""E-NI: negotiation integration test."""
import sys, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))

PASSES, FAILS = [], []

def check(name, cond, detail=''):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def detect(content_str):
    m_reject = re.search(r"\[NEGOTIATION:REJECT\]\s*(.+)", content_str)
    m_counter = re.search(r"\[NEGOTIATION:COUNTER_OFFER\]\s*(.+)", content_str)
    m_delegate = re.search(r"\[NEGOTIATION:DELEGATE:(\S+)\]", content_str)
    if m_reject:
        return {"type": "reject", "reason": m_reject.group(1)[:200]}
    elif m_delegate:
        return {"type": "delegate", "target": m_delegate.group(1)}
    elif m_counter:
        return {"type": "counter_offer", "conditions": m_counter.group(1)[:200]}
    return None

def main():
    # REJECT signal
    r = detect('Task done. [NEGOTIATION:REJECT] I cannot do frontend work')
    check("NI-1 reject detected", r and r["type"] == "reject", str(r))

    # COUNTER_OFFER signal
    r2 = detect('[NEGOTIATION:COUNTER_OFFER] Need security agent support')
    check("NI-2 counter detected", r2 and r2["type"] == "counter_offer", str(r2))

    # DELEGATE signal
    r3 = detect('[NEGOTIATION:DELEGATE:dsh] This is a security task')
    check("NI-3 delegate detected", r3 and r3["type"] == "delegate" and r3["target"] == "dsh", str(r3))

    # No signal
    r4 = detect('Task completed successfully, output written')
    check("NI-4 no signal", r4 is None, str(r4))

    # Verify interface.py has the integration
    iface = Path(r'C:\Users\33389\workspace\projects\universal-agent-interface\prototype\interface.py').read_text(encoding='utf-8')
    check("NI-5 interface.py has negotiation check", "negotiation_rejected" in iface, "")
    check("NI-6 interface.py detects signals", "NEGOTIATION:REJECT" in iface, "")

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())