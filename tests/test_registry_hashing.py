import hashlib

import pytest

from cgai.hashing import local_weights_digest, weights_digest
from cgai.registry import Registry, RegistryError


def test_digest_is_order_independent_and_matches_definition():
    files = {"b.safetensors": "BB" * 32, "a.safetensors": "aa" * 32}
    expected = hashlib.sha256(
        f"a.safetensors:{'aa' * 32}\nb.safetensors:{'bb' * 32}\n".encode()).hexdigest()
    assert weights_digest(files) == expected
    assert weights_digest(dict(reversed(list(files.items())))) == expected


def test_local_digest_matches_hub_style_digest(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "model-00001.safetensors").write_bytes(b"x")
    (tmp_path / "config.json").write_text("{}")  # not a weight file
    sha = hashlib.sha256(b"x").hexdigest()
    assert local_weights_digest(tmp_path) == weights_digest({"sub/model-00001.safetensors": sha})


def test_empty_digest_rejected():
    with pytest.raises(ValueError):
        weights_digest({})


def test_versions_are_append_only_and_persist(registry):
    v1 = registry.approve("org/a", "1" * 40, "d1", {"m.safetensors": "s1"})
    v2 = registry.approve("org/b", "2" * 40, "d2", {"m.safetensors": "s2"})
    assert (v1.version, v2.version) == (1, 2)
    with pytest.raises(RegistryError):
        registry.approve("org/mirror", "3" * 40, "d1", {"m.safetensors": "s1"})  # same weights
    again = Registry(registry.path)
    assert [v.weights_digest for v in again.state.versions] == ["d1", "d2"]


def test_activate_and_rollback(registry):
    registry.approve("org/a", "1" * 40, "d1", {"m.safetensors": "s1"})
    registry.approve("org/b", "2" * 40, "d2", {"m.safetensors": "s2"})
    with pytest.raises(RegistryError):
        registry.rollback()
    registry.activate(1)
    registry.activate(2)
    assert registry.active_version().weights_digest == "d2"
    assert registry.rollback().version == 1
    assert len(registry.state.versions) == 2  # rollback deletes nothing
    with pytest.raises(RegistryError):
        registry.activate(99)


def test_pause_persists(registry):
    registry.set_paused(True)
    assert Registry(registry.path).state.paused is True
