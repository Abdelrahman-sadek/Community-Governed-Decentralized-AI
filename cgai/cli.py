"""Command line: ``python -m cgai <command>``."""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
from pathlib import Path

from .gateway import Gateway, HttpNodeClient, LocalNodeClient, RateLimiter, make_gateway_server
from .hashing import local_weight_files, weights_digest
from .node import BACKENDS, Node, ToyBackend, fetch_version, make_node_server
from .registry import Registry, RegistryError
from .scout import Candidate, Policy, Scout

DATA = Path(os.environ.get("CGAI_DATA", "data"))


def _registry(a) -> Registry:
    return Registry(a.registry)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2))


def cmd_scout(a):
    reg = _registry(a)
    scout = Scout(Policy(max_weight_bytes=int(a.max_gb * 1024**3)))
    known = {v.weights_digest for v in reg.state.versions}
    cands, rejects = scout.scan(repos=a.repo or None, limit=a.limit, known_digests=known)
    for c in cands:
        print(json.dumps(c.to_json()))
    print(f"{len(cands)} candidate(s), {len(rejects)} rejected", file=sys.stderr)
    if a.verbose:
        for r in rejects:
            print(f"  rejected {r.repo}: {r.reason}", file=sys.stderr)


def cmd_approve(a):
    reg = _registry(a)
    result = Scout(Policy(max_weight_bytes=int(a.max_gb * 1024**3))).inspect(a.repo, a.commit)
    if not isinstance(result, Candidate):
        sys.exit(f"refused: {result.reason}")
    v = reg.approve(result.repo, result.commit, result.weights_digest, result.files, a.note)
    print(f"archived version {v.version}: {v.repo}@{v.commit[:12]} weights {v.weights_digest[:16]}")
    if a.activate:
        reg.activate(v.version)
        print(f"activated version {v.version}")


def cmd_activate(a):
    v = _registry(a).activate(a.version)
    print(f"activated version {v.version} ({v.repo})")


def cmd_rollback(a):
    v = _registry(a).rollback()
    print(f"rolled back to version {v.version} ({v.repo})")


def cmd_pause(a, paused=True):
    _registry(a).set_paused(paused)
    print("paused" if paused else "unpaused")


def cmd_status(a):
    reg = _registry(a)
    _print({"active": reg.state.active, "paused": reg.state.paused,
            "history": reg.state.history,
            "versions": [{"version": v.version, "repo": v.repo, "commit": v.commit,
                          "weights_digest": v.weights_digest} for v in reg.state.versions]})


def cmd_fetch(a):
    reg = _registry(a)
    v = reg.get(a.version) if a.version else reg.active_version()
    if v is None:
        sys.exit("no active version; pass --version")
    path = fetch_version(v, a.store)
    print(f"version {v.version} verified at {path}")


def cmd_node(a):
    node = Node(a.id, _registry(a), a.store, BACKENDS[a.backend])
    print(json.dumps(node.health()))
    srv = make_node_server(node, a.host, a.port)
    print(f"node {a.id} listening on http://{a.host}:{srv.server_address[1]}")
    srv.serve_forever()


def cmd_gateway(a):
    clients = [HttpNodeClient(u) for u in a.nodes.split(",") if u]
    gw = Gateway(clients, _registry(a), reexec_rate=a.reexec)
    srv = make_gateway_server(gw, RateLimiter(a.per_minute), a.host, a.port)
    print(f"gateway listening on http://{a.host}:{srv.server_address[1]} "
          f"({len(clients)} nodes, re-execution {a.reexec:.0%})")
    srv.serve_forever()


class _LyingBackend(ToyBackend):
    """Demo only: a node that runs the right weights but returns wrong answers."""

    def generate(self, prompt, max_new_tokens):
        return "made-up answer"


def cmd_demo(a):
    rng = random.Random(a.seed)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        reg = Registry(tmp / "registry.json")
        model = tmp / "models" / "pending"
        model.mkdir(parents=True)
        (model / "model.safetensors").write_bytes(rng.randbytes(4096))
        files = local_weight_files(model)
        digest = weights_digest(files)
        model.rename(tmp / "models" / digest)
        v = reg.approve("demo/toy-model", "0" * 40, digest, files, "demo")
        reg.activate(v.version)
        print(f"1. Community approved version {v.version} (weights {digest[:16]})")

        nodes = [Node(f"node-{i}", reg, tmp / "models") for i in range(1, 5)]
        nodes.append(Node("node-liar", reg, tmp / "models", _LyingBackend))
        print(f"2. {len(nodes)} independent providers serve it; one of them lies")

        gw = Gateway([LocalNodeClient(n) for n in nodes], reg, reexec_rate=0.3, rng=rng)
        for i in range(a.requests):
            gw.handle(f"question {i}")
        st = gw.status()["nodes"]
        print(f"3. Served {a.requests} free requests, re-checking ~30% on a second node:")
        for nid, s in st.items():
            flag = "  <- excluded" if s["excluded"] else ""
            print(f"     {nid:10} served {s['served']:3}  checked {s['checked']:3}  "
                  f"strikes {s['strikes']}{flag}")

        tampered = tmp / "models" / digest / "model.safetensors"
        tampered.write_bytes(b"poisoned" + tampered.read_bytes()[8:])
        fresh = Node("node-new", reg, tmp / "models")
        print(f"4. Tampered weights on disk -> new node health: {fresh.health()['error']}")

        reg.set_paused(True)
        print(f"5. Governance pause -> node health: {nodes[0].health()['error']}")


def main(argv=None):
    p = argparse.ArgumentParser(prog="cgai", description="Community-Governed Decentralized AI, Phase 0")
    p.add_argument("--registry", default=str(DATA / "registry.json"), help="registry file")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scout", help="list candidate models from Hugging Face")
    s.add_argument("--limit", type=int, default=50)
    s.add_argument("--repo", action="append", help="inspect specific repos instead of recent ones")
    s.add_argument("--max-gb", type=float, default=2.0)
    s.add_argument("-v", "--verbose", action="store_true")
    s.set_defaults(fn=cmd_scout)

    s = sub.add_parser("approve", help="archive a model version (a governance vote in Phase 1)")
    s.add_argument("repo")
    s.add_argument("--commit", default="main", help="revision to pin; resolved to a commit hash")
    s.add_argument("--note", default="")
    s.add_argument("--activate", action="store_true")
    s.add_argument("--max-gb", type=float, default=2.0)
    s.set_defaults(fn=cmd_approve)

    s = sub.add_parser("activate", help="make an archived version active")
    s.add_argument("version", type=int)
    s.set_defaults(fn=cmd_activate)
    sub.add_parser("rollback", help="restore the previously active version").set_defaults(fn=cmd_rollback)
    sub.add_parser("pause", help="emergency pause").set_defaults(fn=cmd_pause)
    sub.add_parser("unpause", help="clear the pause").set_defaults(fn=lambda a: cmd_pause(a, False))
    sub.add_parser("status", help="show the registry").set_defaults(fn=cmd_status)

    s = sub.add_parser("fetch", help="download and verify an approved version")
    s.add_argument("--version", type=int)
    s.add_argument("--store", default=str(DATA / "models"))
    s.set_defaults(fn=cmd_fetch)

    s = sub.add_parser("node", help="run a provider node")
    s.add_argument("--id", required=True)
    s.add_argument("--store", default=str(DATA / "models"))
    s.add_argument("--backend", choices=sorted(BACKENDS), default="transformers")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8100)
    s.set_defaults(fn=cmd_node)

    s = sub.add_parser("gateway", help="run the free-access gateway")
    s.add_argument("--nodes", required=True, help="comma-separated node URLs")
    s.add_argument("--reexec", type=float, default=0.2, help="share of requests re-checked")
    s.add_argument("--per-minute", type=int, default=30, help="free requests per client per minute")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_gateway)

    s = sub.add_parser("demo", help="run an offline demo with toy nodes")
    s.add_argument("--requests", type=int, default=200)
    s.add_argument("--seed", type=int, default=7)
    s.set_defaults(fn=cmd_demo)

    a = p.parse_args(argv)
    try:
        a.fn(a)
    except RegistryError as e:
        sys.exit(f"registry: {e}")


if __name__ == "__main__":
    main()
