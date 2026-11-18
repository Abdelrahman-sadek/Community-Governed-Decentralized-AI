"""Free-access gateway with random re-execution.

Anyone can send a request: no account, payment or token (paper, Section 6).
The gateway:

* routes each request to a random node that serves the approved version;
* if that node fails, tries the next one, so no single node is a point of failure;
* re-executes a random share of requests on a second node and compares the
  answers. On a mismatch a third node breaks the tie and the node in the
  minority gets a strike; after ``max_strikes`` it is no longer used;
* applies a per-client rate limit so free capacity is shared fairly.

Client addresses are used only for rate limiting, kept in memory and never logged.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer
from typing import Protocol

from .node import JsonHandler, Node
from .registry import Registry


class NodeClient(Protocol):
    node_id: str

    def infer(self, prompt: str, max_new_tokens: int) -> dict: ...


class LocalNodeClient:
    def __init__(self, node: Node):
        self.node = node
        self.node_id = node.node_id

    def infer(self, prompt: str, max_new_tokens: int) -> dict:
        return self.node.infer(prompt, max_new_tokens)


class HttpNodeClient:
    def __init__(self, url: str, timeout: float = 120):
        self.url = url.rstrip("/")
        self.node_id = self.url
        self.timeout = timeout

    def infer(self, prompt: str, max_new_tokens: int) -> dict:
        data = json.dumps({"prompt": prompt, "max_new_tokens": max_new_tokens}).encode()
        req = urllib.request.Request(f"{self.url}/infer", data=data,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.load(resp)


class NoNodeAvailable(Exception):
    pass


@dataclass
class NodeRecord:
    client: NodeClient
    strikes: int = 0
    served: int = 0
    checked: int = 0
    failures: int = 0


@dataclass
class Answer:
    output: str
    node_id: str
    version: int
    checked: bool = False   # re-executed on another node
    agreed: bool | None = None
    detail: dict = field(default_factory=dict)


class Gateway:
    def __init__(self, clients: list[NodeClient], registry: Registry,
                 reexec_rate: float = 0.2, max_strikes: int = 3,
                 rng: random.Random | None = None):
        self.nodes = {c.node_id: NodeRecord(c) for c in clients}
        self.registry = registry
        self.reexec_rate = reexec_rate
        self.max_strikes = max_strikes
        self.rng = rng or random.Random()
        self.lock = threading.Lock()

    def eligible(self) -> list[NodeRecord]:
        return [r for r in self.nodes.values() if r.strikes < self.max_strikes]

    def _call(self, rec: NodeRecord, prompt: str, max_new_tokens: int, digest: str) -> dict | None:
        try:
            resp = rec.client.infer(prompt, max_new_tokens)
        except Exception:
            with self.lock:
                rec.failures += 1
            return None
        if resp.get("weights_digest") != digest:  # stale or wrong model: do not use
            with self.lock:
                rec.failures += 1
            return None
        return resp

    def _first_answer(self, order: list[NodeRecord], prompt, max_new_tokens, digest):
        while order:
            rec = order.pop(0)
            resp = self._call(rec, prompt, max_new_tokens, digest)
            if resp is not None:
                return rec, resp
        return None, None

    def handle(self, prompt: str, max_new_tokens: int = 64) -> Answer:
        self.registry.reload()
        if self.registry.state.paused:
            raise NoNodeAvailable("network is paused by governance")
        active = self.registry.active_version()
        if active is None:
            raise NoNodeAvailable("no model has been activated")
        digest = active.weights_digest

        order = self.eligible()
        self.rng.shuffle(order)
        primary, resp = self._first_answer(order, prompt, max_new_tokens, digest)
        if primary is None:
            raise NoNodeAvailable("no node could serve the request")
        with self.lock:
            primary.served += 1
        answer = Answer(resp["output"], primary.client.node_id, active.version)

        if not order or self.rng.random() >= self.reexec_rate:
            return answer

        checker, resp2 = self._first_answer(order, prompt, max_new_tokens, digest)
        if checker is None:
            return answer
        answer.checked = True
        with self.lock:
            primary.checked += 1
            checker.checked += 1
        if resp2["output"] == resp["output"]:
            answer.agreed = True
            return answer

        # Disagreement: ask a third node, majority wins, minority gets a strike.
        answer.agreed = False
        votes = {primary.client.node_id: resp["output"], checker.client.node_id: resp2["output"]}
        referee, resp3 = self._first_answer(order, prompt, max_new_tokens, digest)
        if referee is not None:
            votes[referee.client.node_id] = resp3["output"]
        winner, count = Counter(votes.values()).most_common(1)[0]
        if count >= 2:
            with self.lock:
                for nid, out in votes.items():
                    if out != winner:
                        self.nodes[nid].strikes += 1
            answer.output = winner
            answer.node_id = next(n for n, o in votes.items() if o == winner)
        answer.detail = {"votes": votes, "resolved": count >= 2}
        return answer

    def status(self) -> dict:
        self.registry.reload()
        v = self.registry.active_version()
        return {
            "paused": self.registry.state.paused,
            "active_version": v.version if v else None,
            "nodes": {nid: {"strikes": r.strikes, "served": r.served, "checked": r.checked,
                            "failures": r.failures, "excluded": r.strikes >= self.max_strikes}
                      for nid, r in self.nodes.items()},
        }


class RateLimiter:
    """Token bucket per client: ``per_minute`` requests, refilled continuously."""

    def __init__(self, per_minute: int = 30, clock=time.monotonic):
        self.capacity = float(per_minute)
        self.rate = per_minute / 60.0
        self.clock = clock
        self.buckets: dict[str, tuple[float, float]] = {}
        self.lock = threading.Lock()

    def allow(self, client: str) -> bool:
        now = self.clock()
        with self.lock:
            tokens, last = self.buckets.get(client, (self.capacity, now))
            tokens = min(self.capacity, tokens + (now - last) * self.rate)
            ok = tokens >= 1
            self.buckets[client] = (tokens - 1 if ok else tokens, now)
            if len(self.buckets) > 100_000:  # bound memory
                self.buckets.clear()
            return ok


def make_gateway_server(gw: Gateway, limiter: RateLimiter | None = None,
                        host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    limiter = limiter or RateLimiter()

    class Handler(JsonHandler):
        def do_GET(self):
            if self.path == "/v1/status":
                return self.send_json(200, gw.status())
            self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/v1/generate":
                return self.send_json(404, {"error": "not found"})
            if not limiter.allow(self.client_address[0]):
                return self.send_json(429, {"error": "rate limit: try again shortly"})
            try:
                body = self.read_json()
                prompt = body.get("prompt")
                if not isinstance(prompt, str) or not prompt:
                    raise ValueError("prompt must be a non-empty string")
                a = gw.handle(prompt, int(body.get("max_new_tokens", 64)))
                self.send_json(200, {"output": a.output, "version": a.version,
                                     "node": a.node_id, "checked": a.checked})
            except NoNodeAvailable as e:
                self.send_json(503, {"error": str(e)})
            except (ValueError, TypeError) as e:
                self.send_json(400, {"error": str(e)})

    return ThreadingHTTPServer((host, port), Handler)
