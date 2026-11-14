# Universal Agent Interface (UAI)

> 📖 [中文文档 / Chinese README](README.zh-CN.md)

**Cross-vendor agent collaboration with acceptance governance**: agents from different runtimes (Codex, Claude Code, DeepSeek Harness, any CLI) work under one hub — with enforced acceptance gates so that "an agent says DONE" is never trusted until a third-party verifier signs off.

> Honest scope: proven in a real multi-agent delivery pipeline (4 seats, 3,000+ ledger records, 7 fabricated-completion incidents caught). Single-developer small tasks don't need this — deploy cost > benefit.

## Why

LLM agents fabricate completions. Prompting can't fix it; mechanisms can. UAI separates **execution state** from **acceptance verdicts**:

- `SUCCEEDED` only means "stopped and submitted a claim" — externally projected as "pending verification" until a VERDICT lands.
- Verifier seat is never the implementer (ENFORCED, tested).
- Evidence levels E1–E4; self-reported evidence cannot impersonate independent verification.

## Quickstart (30 minutes, Linux/macOS/WSL)

```bash
git clone https://github.com/3338902669-ops/universal-agent-interface.git
cd universal-agent-interface

# 1. deps
pip install nats-py jsonschema cryptography

# 2. NATS (any server ≥2.9)
nats-server -c prototype/nats-test.conf        # listens 127.0.0.1:43321

# 3. hub
python prototype/hub.py --base prototype --nats-url nats://127.0.0.1:43321

# 4. verify acceptance gate projection (read-only, no deps on runtime)
python stage2/test_projection.py               # 10/10
python stage2/test_a2a_server.py               # 12/12
```

Full suite (24 suites, 300+ cases) and a real-lab end-to-end (A2A request → envelope → hub → real runtime → ledger) live in [`stage2/DEVELOPMENT.md`](stage2/DEVELOPMENT.md).

## What's inside

| Layer | Path | What it does |
|---|---|---|
| Contract | `protocol/contract-v1.md`, `schemas/` | v1 envelope (11 required fields), canonical verdict vocabulary, JSON Schema, conformance suite |
| Prototype | `prototype/` | NATS hub: idempotency, leases+fencing, single-writer resources, ledger hash-chain |
| Governance (stage2) | `stage2/` | A2A projection (AgentCard/Task/events), inbound message/send, SSE stream, webhook push (HMAC), OIDC/Ed25519 verifiers, unified cross-repo model |
| Bridges | `stage2/paseo_uai_bridge.py` | Paseo daemon orchestration ↔ UAI acceptance |

## Architecture in one picture

```
runtime seats (codex / claude / dsh / yours)
        │  provider CLI
   Paseo daemon (or any orchestrator)
        │  envelope (contract v1)
   UAI hub — ledger (append-only, hash-chained) + verdicts
        │  projection (AgentCard / Task / SSE / webhook)
   external A2A consumers
```

Design rule: reuse A2A semantics (AgentCard, task states), replace the transport; acceptance/verdict logic is internal and never outsourced.

## Key invariants (ENFORCED, each with a test)

- SUCCEEDED ≠ passed — projection writes "pending verification" without a verdict
- idempotency treats loss only; fabrication is handled by the verdict seat
- verifier ≠ implementer; unknown side-effect state forbids auto-retry
- no verifier configured → identity stays `asserted`, never upgraded
- `experimental_test_only` gate on event_bridge until a real cross-vendor consumer exists

## Status / honest gaps

- Cross-vendor identity: OIDC verifier ready (production JWKS verified against Microsoft), real IdP issuance is account-side work
- A2A streaming: SSE done; push notifications done (webhook, HMAC); gRPC binding not planned
- event_bridge v1/v2 remain behind `experimental_test_only` until a real external consumer exists

License: see LICENSE. Paseo bridge operates under AGPL-3.0 constraints of the upstream project.
