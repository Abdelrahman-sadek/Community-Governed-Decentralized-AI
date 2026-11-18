"""Resource-provider node.

A node serves the community-approved model and nothing else. On every request
it reads the registry; it refuses to serve while the network is paused, and it
refuses to load weights whose hash does not match the approved digest.

Weights live in a local *model store*, one folder per weights digest
(``<store>/<digest>/``), so a rollback can switch back to an older version
that is already on disk.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from typing import Callable, Protocol

from .hashing import local_weight_files, sha256_file, weights_digest
from .registry import ModelVersion, Registry
from .scout import HF_ENDPOINT, USER_AGENT, http_get_json

MAX_NEW_TOKENS = 256
MAX_BODY_BYTES = 64 * 1024

# Non-weight files a transformers model needs. Nothing executable, nothing pickled.
SUPPORT_FILES = frozenset({
    "config.json", "generation_config.json", "tokenizer.json", "tokenizer_config.json",
    "special_tokens_map.json", "vocab.json", "merges.txt", "tokenizer.model",
})


class IntegrityError(Exception):
    """Local weights do not match the approved digest."""


class Paused(Exception):
    """The emergency pause is active."""


class NotReady(Exception):
    """The active version is not available on this node."""


class Backend(Protocol):
    def generate(self, prompt: str, max_new_tokens: int) -> str: ...


BackendFactory = Callable[[Path, ModelVersion], Backend]


class ToyBackend:
    """Deterministic stand-in for a model, for tests and demos without a GPU.

    Its output depends only on the approved weights and the prompt, so two honest
    nodes always agree, which is what re-execution checks rely on.
    """

    def __init__(self, model_dir: Path, version: ModelVersion):
        self.digest = version.weights_digest

    def generate(self, prompt: str, max_new_tokens: int) -> str:
        h = hashlib.sha256(f"{self.digest}|{max_new_tokens}|{prompt}".encode()).hexdigest()
        return f"toy-answer-{h[:32]}"


class TransformersBackend:
    """Runs a real model with Hugging Face transformers (``pip install .[model]``).

    Greedy decoding keeps outputs reproducible so other nodes can re-check them.
    Remote code is never executed and only safetensors weights are loaded.
    """

    def __init__(self, model_dir: Path, version: ModelVersion):
        import torch  # noqa: F401  (optional dependency)
        from transformers import AutoModelForCausalLM, AutoTokenizer

        kw = dict(local_files_only=True, trust_remote_code=False)
        self.tok = AutoTokenizer.from_pretrained(model_dir, **kw)
        self.model = AutoModelForCausalLM.from_pretrained(model_dir, use_safetensors=True, **kw)
        self.model.eval()

    def generate(self, prompt: str, max_new_tokens: int) -> str:
        import torch

        if getattr(self.tok, "chat_template", None):
            text = self.tok.apply_chat_template(
                [{"role": "user", "content": prompt}], tokenize=False, add_generation_prompt=True)
        else:
            text = prompt
        inputs = self.tok(text, return_tensors="pt")
        with torch.no_grad():
            out = self.model.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        return self.tok.decode(out[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)


BACKENDS: dict[str, BackendFactory] = {"toy": ToyBackend, "transformers": TransformersBackend}


# ---------------------------------------------------------------------------
# Fetching weights
# ---------------------------------------------------------------------------

def _safe_relpath(name: str) -> PurePosixPath:
    p = PurePosixPath(name)
    if p.is_absolute() or ".." in p.parts or not p.parts:
        raise IntegrityError(f"unsafe file name: {name!r}")
    return p


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    tmp = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tmp, "wb") as f:
        shutil.copyfileobj(resp, f, 1 << 20)
    tmp.replace(dest)


def fetch_version(version: ModelVersion, store: str | Path, endpoint: str = HF_ENDPOINT,
                  get_json: Callable[[str], object] = http_get_json,
                  download: Callable[[str, Path], None] = _download) -> Path:
    """Download an approved version into ``<store>/<digest>/`` and verify every weight file.

    Files are fetched at the pinned commit, never at ``main``. A file whose hash does
    not match the registry is deleted and the fetch fails.
    """
    root = Path(store) / version.weights_digest
    root.mkdir(parents=True, exist_ok=True)
    base = f"{endpoint.rstrip('/')}/{urllib.parse.quote(version.repo, safe='/')}/resolve/{version.commit}"

    info = get_json(f"{endpoint.rstrip('/')}/api/models/"
                    f"{urllib.parse.quote(version.repo, safe='/')}/revision/{version.commit}")
    names = {s.get("rfilename", "") for s in info.get("siblings", [])}  # type: ignore[union-attr]
    wanted = sorted(n for n in names if PurePosixPath(n).name in SUPPORT_FILES) + sorted(version.files)

    for name in wanted:
        rel = _safe_relpath(name)
        dest = root.joinpath(*rel.parts)
        expected = version.files.get(name)
        if dest.exists() and (expected is None or sha256_file(dest) == expected):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        download(f"{base}/{urllib.parse.quote(name)}", dest)
        if expected is not None and sha256_file(dest) != expected:
            dest.unlink()
            raise IntegrityError(f"{name}: hash mismatch, file removed")
    verify_model_dir(root, version)
    return root


def verify_model_dir(model_dir: Path, version: ModelVersion) -> None:
    files = local_weight_files(model_dir)
    if files != version.files or weights_digest(files) != version.weights_digest:
        raise IntegrityError(
            f"weights in {model_dir} do not match approved version {version.version}")


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class Node:
    def __init__(self, node_id: str, registry: Registry, store: str | Path,
                 backend: BackendFactory = ToyBackend):
        self.node_id = node_id
        self.registry = registry
        self.store = Path(store)
        self.backend_factory = backend
        self.loaded: ModelVersion | None = None
        self._backend: Backend | None = None
        self._lock = threading.Lock()

    def _sync(self) -> ModelVersion:
        """Follow the registry: load the active version if it changed."""
        self.registry.reload()
        if self.registry.state.paused:
            raise Paused("network is paused by governance")
        active = self.registry.active_version()
        if active is None:
            raise NotReady("no model has been activated")
        if self.loaded is None or self.loaded.weights_digest != active.weights_digest:
            model_dir = self.store / active.weights_digest
            if not model_dir.is_dir():
                raise NotReady(f"version {active.version} not fetched on this node")
            verify_model_dir(model_dir, active)  # refuse anything that does not match
            self._backend = self.backend_factory(model_dir, active)
            self.loaded = active
        return active

    def health(self) -> dict:
        with self._lock:
            try:
                v = self._sync()
                return {"node_id": self.node_id, "ok": True, "version": v.version,
                        "weights_digest": v.weights_digest}
            except (Paused, NotReady, IntegrityError) as e:
                return {"node_id": self.node_id, "ok": False, "error": str(e)}

    def infer(self, prompt: str, max_new_tokens: int = 64) -> dict:
        if not isinstance(prompt, str) or not prompt:
            raise ValueError("prompt must be a non-empty string")
        max_new_tokens = max(1, min(int(max_new_tokens), MAX_NEW_TOKENS))
        with self._lock:
            v = self._sync()
            output = self._backend.generate(prompt, max_new_tokens)  # type: ignore[union-attr]
        return {"node_id": self.node_id, "version": v.version,
                "weights_digest": v.weights_digest, "output": output}


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class JsonHandler(BaseHTTPRequestHandler):
    server_version = "cgai/0.1"

    def log_message(self, fmt, *args):  # keep request logs (and client IPs) out of stdout
        pass

    def send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY_BYTES:
            raise ValueError("request too large")
        body = json.loads(self.rfile.read(n) or b"{}")
        if not isinstance(body, dict):
            raise ValueError("expected a JSON object")
        return body


def make_node_server(node: Node, host: str = "127.0.0.1", port: int = 0) -> ThreadingHTTPServer:
    class Handler(JsonHandler):
        def do_GET(self):
            if self.path == "/health":
                return self.send_json(200, node.health())
            self.send_json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/infer":
                return self.send_json(404, {"error": "not found"})
            try:
                body = self.read_json()
                self.send_json(200, node.infer(body.get("prompt"), body.get("max_new_tokens", 64)))
            except Paused as e:
                self.send_json(503, {"error": str(e)})
            except (NotReady, IntegrityError) as e:
                self.send_json(409, {"error": str(e)})
            except (ValueError, TypeError) as e:
                self.send_json(400, {"error": str(e)})

    return ThreadingHTTPServer((host, port), Handler)
