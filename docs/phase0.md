# Phase 0: prototype without a token

This is the first code for the paper in the [README](../README.md). It follows the paper's roadmap (Section 10): **build what works before adding a token**. Phase 0 has no blockchain. It shows that independent providers can serve the same approved model, check each other, and give everyone free access with no central operator.

## What is built

| Paper | Code | What it does |
| --- | --- | --- |
| Scout (§5, step 1) | [`cgai/scout.py`](../cgai/scout.py) | Reads the Hugging Face Hub. Keeps only public, ungated models with an approved open license, safetensors weights, no custom code, within a size limit. Pins each one to a commit hash and computes its weights digest from the SHA-256 values the Hub reports. Proposes only; never approves. |
| Model registry and Model Version Archive (§4, §5) | [`cgai/registry.py`](../cgai/registry.py) | Append-only list of approved versions, the active version, rollback history and the emergency pause. Stand-in for the Phase 1 governance contract. |
| Weights identity (§5) | [`cgai/hashing.py`](../cgai/hashing.py) | A model is its weights hash, not its repo name. |
| Provider node (§3, §5) | [`cgai/node.py`](../cgai/node.py) | Downloads the approved files at the pinned commit, verifies every weight file, and refuses to serve weights that do not match. Follows activation, rollback and pause on every request. Runs real models with `trust_remote_code=False` and greedy decoding. |
| Output verification (§7, P0) | [`cgai/gateway.py`](../cgai/gateway.py) | Re-runs a random share of requests on a second node. On a mismatch a third node breaks the tie; the node in the minority gets a strike and is excluded after three. |
| Free access (§6) | [`cgai/gateway.py`](../cgai/gateway.py) | No account, payment or token. A per-client rate limit shares free capacity. Client addresses stay in memory and are never logged. Fails over to another node if one is down. |

## Weights digest

A model may be split into several files, so its identity is:

```
weights_digest = sha256( for each *.safetensors file, sorted by name:
                         "<file name>:<file sha256>\n" )
```

The scout computes it from Hub metadata before downloading anything; a node recomputes it from disk.

## Quick start

Requires Python 3.10 or newer. The core has no dependencies.

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows; use .venv/bin/activate on Linux and macOS
pip install -e ".[dev]"
pytest
python -m cgai demo
```

`demo` runs offline: five toy nodes, one of which returns made-up answers. It is caught by re-execution and excluded, a tampered weights file is refused, and the pause stops inference.

## Running a real model

```bash
pip install -e ".[model]"                     # torch and transformers
python -m cgai scout --limit 30 -v            # list candidates
python -m cgai approve HuggingFaceTB/SmolLM2-135M-Instruct --activate
python -m cgai fetch                          # download at the pinned commit and verify
python -m cgai node --id n1 --port 8101 &
python -m cgai node --id n2 --port 8102 &
python -m cgai gateway --nodes http://127.0.0.1:8101,http://127.0.0.1:8102
curl -X POST http://127.0.0.1:8000/v1/generate -d '{"prompt": "What is a DAO?"}'
```

Other commands: `status`, `activate <version>`, `rollback`, `pause`, `unpause`. Data goes to `./data` unless `CGAI_DATA` is set.

In Phase 0, `approve`, `activate`, `rollback` and `pause` are run by whoever holds the registry file. That is a central point of control, and it is exactly what Phase 1 removes: the same operations become proposals, votes and a timelock in the governance contract, with the council limited to pause.

## Known limits of Phase 0

- **One registry file.** Nodes and the gateway share it on one machine. Phase 1 replaces it with the on-chain contract that every node reads.
- **One gateway.** Run several for resilience; Section 7.1 of the paper (several gateways, IPFS front end, P2P apps) is not built yet.
- **Unchecked answers can be wrong.** A dishonest node is caught only on re-checked requests. With a 20% re-execution rate it is excluded after a few lies, but answers that were not re-checked before that reach users. Raise `--reexec` for critical use.
- **Exact matching.** Re-execution compares outputs exactly. Greedy decoding is reproducible on the same software and hardware, but different GPUs or library versions can produce different tokens. Comparing with a tolerance, or fixing a reference runtime, is open work.
- **Only weights are hashed.** Config and tokenizer files are fetched at the pinned commit but not yet part of the digest.
- **No staged rollout yet.** Activation switches all nodes at once. The 10% → 50% → 100% rollout with automatic rollback is next.
- **No evaluation step.** Candidates go straight from the scout to `approve`. The decentralized evaluation with signed reports on IPFS is next.

## Next steps

1. Staged rollout with automatic rollback on error-rate or safety signals.
2. Decentralized evaluation: random nodes run benchmark and safety suites and sign the report.
3. Phase 1: the `ModelGovernance` contract on a test network, replacing `registry.py`.
