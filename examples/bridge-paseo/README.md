# Example: Paseo orchestration bridge

Paseo daemon manages harness sessions/workspaces; UAI provides acceptance governance.
Full implementation: stage2/paseo_uai_bridge.py (works standalone).

Prereqs:
1. npm i -g @getpaseo/cli && paseo daemon start (ws://127.0.0.1:6767)
2. UAI hub on nats://127.0.0.1:43321
3. A provider CLI logged in (codex/claude)

Run: python stage2/paseo_uai_bridge.py "your instruction"
Expected: {"uai_task_id":..., "paseo_agent_id":..., "reply": "..."} — dual credentials bound.
