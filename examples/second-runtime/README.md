# Example: adding a second vendor runtime

The contract claims "cross-vendor" — this is how you actually prove it.
Copy runtime_adapters/dsh_adapter.py (150 lines): translates codex-style argv
into your CLI's interface. Launch interface.py with --executor runtime
--runtime-executable <your adapter>. Then run test_a05_second_runtime.py —
parse/failure layers must always pass; the contract layer requires a real
runtime call returning your marker string.
