import json
import random
import threading
import urllib.error
import urllib.request

import pytest

from cgai.gateway import (Gateway, HttpNodeClient, LocalNodeClient, NoNodeAvailable,
                          RateLimiter, make_gateway_server)
from cgai.hashing import sha256_file
from cgai.node import (IntegrityError, Node, NotReady, Paused, ToyBackend, fetch_version,
                       make_node_server)
from conftest import make_model


class Liar(ToyBackend):
    def generate(self, prompt, max_new_tokens):
        return "wrong"


# ---- node -----------------------------------------------------------------

def test_honest_nodes_agree(registry, store, active):
    a, b = Node("a", registry, store), Node("b", registry, store)
    ra, rb = a.infer("hello"), b.infer("hello")
    assert ra["output"] == rb["output"]
    assert ra["weights_digest"] == active.weights_digest


def test_node_refuses_tampered_weights(registry, store, active):
    (store / active.weights_digest / "model.safetensors").write_bytes(b"poison")
    with pytest.raises(IntegrityError):
        Node("a", registry, store).infer("hi")


def test_node_refuses_extra_weight_file(registry, store, active):
    (store / active.weights_digest / "extra.safetensors").write_bytes(b"x")
    with pytest.raises(IntegrityError):
        Node("a", registry, store).infer("hi")


def test_node_obeys_pause(registry, store, active):
    node = Node("a", registry, store)
    node.infer("hi")
    registry.set_paused(True)
    with pytest.raises(Paused):
        node.infer("hi")
    registry.set_paused(False)
    node.infer("hi")


def test_node_follows_activation_and_rollback(registry, store, active):
    node = Node("a", registry, store)
    out1 = node.infer("q")["output"]
    d2, f2 = make_model(store, b"weights-v2")
    v2 = registry.approve("org/model", "b" * 40, d2, f2)
    registry.activate(v2.version)
    r = node.infer("q")
    assert (r["version"], r["output"] != out1) == (2, True)
    registry.rollback()
    assert node.infer("q")["output"] == out1


def test_node_not_ready_without_weights(registry, store):
    v = registry.approve("org/model", "a" * 40, "f" * 64, {"model.safetensors": "0" * 64})
    registry.activate(v.version)
    with pytest.raises(NotReady):
        Node("a", registry, store).infer("hi")


def test_max_new_tokens_is_capped(registry, store, active):
    class Spy(ToyBackend):
        seen = []

        def generate(self, prompt, n):
            Spy.seen.append(n)
            return "x"

    Node("a", registry, store, Spy).infer("hi", 10**6)
    assert Spy.seen == [256]


# ---- fetch ----------------------------------------------------------------

def _fake_hub(payloads):
    def get_json(url):
        return {"siblings": [{"rfilename": n} for n in payloads]}

    def download(url, dest):
        dest.write_bytes(payloads[url.rsplit("/", 1)[1]])
    return get_json, download


def test_fetch_downloads_at_commit_and_verifies(registry, tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "model.safetensors").write_bytes(b"real")
    from cgai.hashing import local_weight_files, weights_digest
    files = local_weight_files(src)
    v = registry.approve("org/m", "c" * 40, weights_digest(files), files)
    get_json, download = _fake_hub({"model.safetensors": b"real", "config.json": b"{}",
                                     "evil.py": b"import os"})
    out = fetch_version(v, tmp_path / "store", get_json=get_json, download=download)
    assert sorted(p.name for p in out.iterdir()) == ["config.json", "model.safetensors"]

    get_json, download = _fake_hub({"model.safetensors": b"swapped"})
    with pytest.raises(IntegrityError):
        fetch_version(v, tmp_path / "store2", get_json=get_json, download=download)
    assert not (tmp_path / "store2" / v.weights_digest / "model.safetensors").exists()


def test_fetch_rejects_path_traversal(registry, tmp_path):
    v = registry.approve("org/m", "c" * 40, "d" * 64, {"../escape.safetensors": "0" * 64})
    get_json, download = _fake_hub({})
    with pytest.raises(IntegrityError):
        fetch_version(v, tmp_path / "store", get_json=get_json, download=download)


# ---- gateway --------------------------------------------------------------

def test_gateway_excludes_dishonest_node(registry, store, active):
    nodes = [Node(f"h{i}", registry, store) for i in range(3)] + [Node("liar", registry, store, Liar)]
    gw = Gateway([LocalNodeClient(n) for n in nodes], registry, reexec_rate=1.0,
                 rng=random.Random(1))
    answers = [gw.handle(f"q{i}") for i in range(60)]
    st = gw.status()["nodes"]
    assert st["liar"]["excluded"] is True
    assert all(not s["excluded"] for nid, s in st.items() if nid != "liar")
    # with every request checked and a third node to break ties, users never get the lie
    assert all(a.output != "wrong" for a in answers)


def test_gateway_fails_over_and_skips_wrong_version(registry, store, active):
    class Down:
        node_id = "down"

        def infer(self, *a):
            raise ConnectionError("offline")

    class Stale:
        node_id = "stale"

        def infer(self, *a):
            return {"output": "old", "weights_digest": "0" * 64}

    good = LocalNodeClient(Node("good", registry, store))
    gw = Gateway([Down(), Stale(), good], registry, reexec_rate=0, rng=random.Random(3))
    for i in range(10):
        assert gw.handle(f"q{i}").node_id == "good"


def test_gateway_respects_pause(registry, store, active):
    gw = Gateway([LocalNodeClient(Node("a", registry, store))], registry)
    registry.set_paused(True)
    with pytest.raises(NoNodeAvailable):
        gw.handle("hi")


def test_rate_limiter():
    t = [0.0]
    rl = RateLimiter(per_minute=2, clock=lambda: t[0])
    assert [rl.allow("x"), rl.allow("x"), rl.allow("x")] == [True, True, False]
    assert rl.allow("y") is True  # other clients unaffected
    t[0] += 30  # one token back
    assert rl.allow("x") is True


# ---- HTTP end to end -------------------------------------------------------

def _serve(srv):
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_http_end_to_end(registry, store, active):
    servers = [make_node_server(Node(f"n{i}", registry, store)) for i in range(2)]
    urls = [_serve(s) for s in servers]
    gw = Gateway([HttpNodeClient(u) for u in urls], registry, reexec_rate=1.0)
    gsrv = make_gateway_server(gw, RateLimiter(per_minute=3))
    g = _serve(gsrv)
    try:
        status, body = _post(f"{g}/v1/generate", {"prompt": "hello"})
        assert status == 200 and body["checked"] is True and body["version"] == 1
        assert _post(f"{g}/v1/generate", {"prompt": ""})[0] == 400
        assert _post(f"{g}/v1/generate", {"prompt": "x"})[0] == 200
        assert _post(f"{g}/v1/generate", {"prompt": "x"})[0] == 429  # 4th request this minute
        registry.set_paused(True)
        assert _post(f"{urls[0]}/infer", {"prompt": "x"})[0] == 503
    finally:
        for s in servers + [gsrv]:
            s.shutdown()
