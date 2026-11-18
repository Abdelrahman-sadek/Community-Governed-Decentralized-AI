import os
from pathlib import Path

import pytest

from cgai.hashing import local_weight_files, weights_digest
from cgai.registry import Registry


def make_model(store: Path, content: bytes = b"weights-v1") -> tuple[str, dict]:
    tmp = store / "incoming"
    tmp.mkdir(parents=True)
    (tmp / "model.safetensors").write_bytes(content)
    files = local_weight_files(tmp)
    digest = weights_digest(files)
    tmp.rename(store / digest)
    return digest, files


@pytest.fixture
def registry(tmp_path) -> Registry:
    return Registry(tmp_path / "registry.json")


@pytest.fixture
def store(tmp_path) -> Path:
    p = tmp_path / "models"
    p.mkdir()
    return p


@pytest.fixture
def active(registry, store):
    """Registry with one approved, active model present in the store."""
    digest, files = make_model(store)
    v = registry.approve("org/model", "a" * 40, digest, files)
    registry.activate(v.version)
    return v
