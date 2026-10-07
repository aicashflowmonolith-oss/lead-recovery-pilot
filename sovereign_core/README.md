# Sovereign Core

A clean-slate, dependency-free autonomous execution core designed to run on weak hardware.

## Principles

- Local-first canonical state in SQLite.
- Durable tasks and events.
- Explicit policy/approval gates.
- Every action is audited.
- Execution is followed by verification.
- Failures retry with bounded backoff.
- No third-party Python packages are required.
- External tools are adapters, not the source of truth.

## Run

```powershell
python monolith.py init
python monolith.py serve --host 127.0.0.1 --port 8765
```

In another terminal:

```powershell
python monolith.py submit --kind echo --payload "{\"text\":\"hello\"}"
python monolith.py worker --once
python monolith.py status
```

## Test

```powershell
python -m unittest -v
```

The initial core intentionally starts small. Capabilities are added as registered adapters and must pass policy and verification before becoming autonomous.
