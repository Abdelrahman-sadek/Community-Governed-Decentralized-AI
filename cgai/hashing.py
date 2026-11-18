"""Weight hashing.

A model's identity is the hash of its weights, not its repository name
(paper, Section 5). A model may be split into several safetensors shards, so
the *weights digest* is defined as the SHA-256 of the sorted lines
``"<file name>:<file sha256>\\n"`` over every ``*.safetensors`` file.

The Hugging Face Hub reports the SHA-256 of every LFS file, so the scout can
compute the digest before anything is downloaded, and a node can recompute it
from the files on disk.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

WEIGHT_SUFFIX = ".safetensors"
_CHUNK = 1 << 20


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def weights_digest(files: dict[str, str]) -> str:
    """Digest of a set of weight files, given as ``{relative_name: sha256}``."""
    if not files:
        raise ValueError("no weight files")
    lines = "".join(f"{name}:{sha.lower()}\n" for name, sha in sorted(files.items()))
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def local_weight_files(model_dir: str | Path) -> dict[str, str]:
    """``{relative_posix_name: sha256}`` for every safetensors file under ``model_dir``."""
    root = Path(model_dir)
    out = {}
    for p in sorted(root.rglob(f"*{WEIGHT_SUFFIX}")):
        if p.is_file():
            out[p.relative_to(root).as_posix()] = sha256_file(p)
    return out


def local_weights_digest(model_dir: str | Path) -> str:
    return weights_digest(local_weight_files(model_dir))
