# Universal Agent Interface

Universal Agent Interface is a vendor-neutral contract and reference implementation for coordinating multiple heterogeneous agent runtimes. Version 0.2 defines both a control plane for registration, routing, state transitions, approvals, and verdicts, and a data plane for durable events, result transport, and reconnect recovery.

The project addresses three recurring coordination problems:

- **Dispatch:** turn free-form delegation into a validated task envelope with a target, deadline, idempotency key, resource key, and risk class.
- **Handoff:** preserve the instruction byte-for-byte, keep delivery at least once, and prevent duplicate execution.
- **Completion:** keep an executor's declaration separate from an independent verdict. A successful execution state means the work stopped and produced a declaration; it does not by itself mean the task is complete.

The v0.2 contract also adds connection registration with an echo check, leases and fencing generations, durable event replay, resource-level serialization, a human approval boundary for external actions, and a fixed six-value verdict vocabulary.

## Repository layout

```text
.
├── README.md
├── LICENSE
├── docs/
│   └── spec-v0.2.md
├── prototype/
│   ├── core.py
│   ├── hub.py
│   ├── interface.py
│   ├── lab_client.py
│   └── test_acceptance.py
└── adapters/
    └── verification-seat/
        ├── core.py
        ├── hub.py
        ├── interface.py
        ├── test_acceptance.py
        ├── test_contract_edges.py
        └── verify_lab.py
```

`prototype/` is the reference lab implementation. `adapters/` contains the verification-seat adapter with stricter contract edge checks. Both use NATS subjects as the transport for the data plane while keeping the contract independent of that transport choice.

## Quick start

Requirements:

- Python 3.10 or newer
- PowerShell on Windows
- NATS Server 2.x
- The Python package `nats-py`
- A compatible agent CLI exposed through `AGENT_RUNTIME_EXECUTABLE` when exercising the runtime-backed acceptance case

Install the Python dependency and make the NATS executable available:

```powershell
python -m pip install nats-py
$env:NATS_SERVER = 'C:\tools\nats-server.exe'  # replace with your installed path
```

Run the reference lab:

```powershell
powershell -NoProfile -File .\prototype\start_lab.ps1
python .\prototype\test_acceptance.py --base .\prototype
powershell -NoProfile -File .\prototype\stop_lab.ps1
```

The full acceptance harness includes a real 300-second broker outage. Run the verification-seat adapter checks the same way:

```powershell
$adapterDir = (Get-ChildItem .\adapters -Directory | Select-Object -First 1).FullName
powershell -NoProfile -File (Join-Path $adapterDir 'start_lab.ps1')
python (Join-Path $adapterDir 'test_acceptance.py') --base $adapterDir
python (Join-Path $adapterDir 'test_contract_edges.py') --base $adapterDir
powershell -NoProfile -File (Join-Path $adapterDir 'stop_lab.ps1')
python (Join-Path $adapterDir 'verify_lab.py') --base $adapterDir
```

The labs bind only to loopback ports. They create their own local tokens, ledger, outbox, logs, and evidence directories at runtime. Those paths are ignored by Git and must not be committed.

This repository is a lab-grade reference implementation. It does not include production communication code, production seat configuration, production credentials, or the operations lab that is still being migrated.

## Relationship to A2A and MCP

A2A describes agent cards and task lifecycles across agent systems. Version 0.2 covers related concepts, with additional requirements for artifact fingerprints and read-back, strong links and replay, lease fencing, and a verdict layer separate from execution state. A future implementation may reuse A2A field names where they fit; conflicts resolve to this contract.

MCP connects models and runtimes to tools and resources. Tool invocation belongs inside an interface implementation and is outside this contract. MCP and this project occupy different layers and do not replace each other.

## Security boundary

Authentication material is represented by references and hashes. The contract requires interface tokens to stay local, approvals to be bound to one envelope and action, and external actions to pass the approval gate. The lab provisions disposable tokens and a lab-only approval signing key at runtime. Never commit `.lab-tokens/`, `data/`, `logs/`, `evidence/`, `runtime/`, private keys, or captured credentials.

## License

Licensed under the [Apache License 2.0](LICENSE).
