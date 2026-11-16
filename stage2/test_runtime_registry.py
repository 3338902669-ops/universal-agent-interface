"""E-REG: runtime registry test."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from runtime_registry import RuntimeRegistry, RuntimeEntry, RegistryError

PASSES, FAILS = [], []

def check(name, cond, detail=""):
    (PASSES if cond else FAILS).append(name)
    print("%s  %-58s %s" % ("PASS" if cond else "FAIL", name, detail))

def main():
    work = Path(tempfile.mkdtemp())

    cfg = work / "registry.json"
    cfg.write_text(json.dumps({
        "runtimes": [
            {"id": "codex", "command": "codex", "type": "native_cli", "description": "OpenAI Codex"},
            {"id": "claude", "command": "claude", "type": "native_cli", "description": "Claude Code"},
            {"id": "dsh", "command": "python dsh_adapter.py", "type": "python_adapter", "description": "DSH"},
        ]
    }), encoding="utf-8")
    reg = RuntimeRegistry.load(cfg)
    check("REG-1 config load", len(reg.entries) == 3, str(len(reg.entries)))

    results = reg.validate_all()
    check("REG-2 validate_all", len(results) == 3, str(len(results)))
    ok_count = sum(1 for r in results if r["ok"])
    check("REG-3 at least one ok", ok_count >= 1, str(results))

    hub_base = work / "hub"
    hub_base.mkdir(exist_ok=True)
    (hub_base / "data").mkdir(exist_ok=True)
    prov = reg.provision_all(hub_base)
    check("REG-4 provision tokens", len(prov["created"]) == 3, str(prov))
    check("REG-5 token file", (hub_base / ".lab-tokens" / "codex.token").exists(), "")
    check("REG-6 catalog", (hub_base / "data" / "token-hashes.json").exists(), "")

    cmds = reg.launch_commands(hub_base, "nats://127.0.0.1:43321")
    check("REG-7 launch cmds", len(cmds) == 3, str(len(cmds)))
    check("REG-8 has interface-id", all("--interface-id" in c["cmd"] for c in cmds), "")
    check("REG-9 has runtime-executable", all("--runtime-executable" in c["cmd"] for c in cmds), "")

    print()
    print("RESULT: %d/%d passed" % (len(PASSES), len(PASSES) + len(FAILS)))
    return 0 if not FAILS else 1

if __name__ == "__main__":
    sys.exit(main())
