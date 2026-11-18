"""Model registry and Model Version Archive (Phase 0 stand-in).

In Phase 1 this state lives in the on-chain ``ModelGovernance`` contract and
changes only through proposal, vote and timelock. Phase 0 has no blockchain
(paper, Section 10), so the same state is kept in a JSON file with the same
rules:

* versions are append-only: approving a model never deletes an older one,
  so any past version can be restored, compared or audited;
* a version is identified by its weights digest, not by its source;
* ``paused`` stops all inference until it is cleared;
* rollback re-activates the previously active version.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class ModelVersion:
    version: int
    repo: str
    commit: str
    weights_digest: str
    files: dict[str, str]  # weight file name -> sha256
    approved_at: float
    note: str = ""


@dataclass
class RegistryState:
    versions: list[ModelVersion] = field(default_factory=list)
    active: int | None = None
    paused: bool = False
    history: list[int] = field(default_factory=list)  # previously active versions


class Registry:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.state = self._load()

    # ---- persistence -------------------------------------------------
    def _load(self) -> RegistryState:
        if not self.path.exists():
            return RegistryState()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return RegistryState(
            versions=[ModelVersion(**v) for v in raw.get("versions", [])],
            active=raw.get("active"),
            paused=bool(raw.get("paused", False)),
            history=list(raw.get("history", [])),
        )

    def reload(self) -> None:
        self.state = self._load()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "versions": [asdict(v) for v in self.state.versions],
            "active": self.state.active,
            "paused": self.state.paused,
            "history": self.state.history,
        }
        # Write atomically so a node never reads a half-written file.
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self.path)

    # ---- queries -----------------------------------------------------
    def get(self, version: int) -> ModelVersion:
        for v in self.state.versions:
            if v.version == version:
                return v
        raise RegistryError(f"unknown version {version}")

    def find_digest(self, digest: str) -> ModelVersion | None:
        return next((v for v in self.state.versions if v.weights_digest == digest), None)

    def active_version(self) -> ModelVersion | None:
        return None if self.state.active is None else self.get(self.state.active)

    # ---- changes (governance decisions in Phase 1) -------------------
    def approve(self, repo: str, commit: str, weights_digest: str,
                files: dict[str, str], note: str = "") -> ModelVersion:
        if self.find_digest(weights_digest):
            raise RegistryError(f"weights {weights_digest[:12]} already archived")
        version = ModelVersion(
            version=len(self.state.versions) + 1,
            repo=repo, commit=commit, weights_digest=weights_digest,
            files=dict(files), approved_at=time.time(), note=note,
        )
        self.state.versions.append(version)
        self.save()
        return version

    def activate(self, version: int) -> ModelVersion:
        v = self.get(version)
        if self.state.active == version:
            return v
        if self.state.active is not None:
            self.state.history.append(self.state.active)
        self.state.active = version
        self.save()
        return v

    def rollback(self) -> ModelVersion:
        if not self.state.history:
            raise RegistryError("no previous version to roll back to")
        self.state.active = self.state.history.pop()
        self.save()
        return self.get(self.state.active)

    def set_paused(self, paused: bool) -> None:
        self.state.paused = paused
        self.save()
