"""Scout: discover candidate models on the Hugging Face Hub.

Discovery is automated; adoption is a community decision (paper, Section 5).
The scout only *proposes* candidates. It never approves or activates one.

Filters applied to every candidate:

* public, not gated and not disabled;
* an approved open license;
* weights in safetensors format only;
* no custom Python code (nodes run with ``trust_remote_code=False``);
* total weight size within the network's hardware budget.

Each candidate is pinned to an exact commit hash and identified by its
weights digest, computed from the SHA-256 values the Hub reports.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .hashing import WEIGHT_SUFFIX, weights_digest

HF_ENDPOINT = "https://huggingface.co"
USER_AGENT = "cgai-scout/0.1"

DEFAULT_LICENSES = frozenset({"apache-2.0", "mit", "bsd-3-clause", "cc-by-4.0", "openrail"})


@dataclass(frozen=True)
class Policy:
    allowed_licenses: frozenset[str] = DEFAULT_LICENSES
    max_weight_bytes: int = 2 * 1024**3  # 2 GiB: fits commodity provider hardware
    pipeline_tag: str = "text-generation"


@dataclass(frozen=True)
class Candidate:
    repo: str
    commit: str
    license: str
    weight_bytes: int
    weights_digest: str
    files: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict:
        return {
            "repo": self.repo, "commit": self.commit, "license": self.license,
            "weight_bytes": self.weight_bytes, "weights_digest": self.weights_digest,
            "files": self.files,
        }


@dataclass(frozen=True)
class Rejection:
    repo: str
    reason: str


def _license_of(info: dict) -> str | None:
    card = info.get("cardData") or {}
    lic = card.get("license")
    if isinstance(lic, list):
        lic = lic[0] if lic else None
    if lic:
        return str(lic).lower()
    for tag in info.get("tags") or []:
        if tag.startswith("license:"):
            return tag.split(":", 1)[1].lower()
    return None


def evaluate(info: dict, policy: Policy = Policy()) -> Candidate | Rejection:
    """Apply the policy to one ``/api/models/{repo}/revision/{rev}?blobs=true`` response."""
    repo = info.get("id") or info.get("modelId") or "?"
    if info.get("private") or info.get("disabled"):
        return Rejection(repo, "private or disabled")
    if info.get("gated"):
        return Rejection(repo, "gated")
    commit = info.get("sha")
    if not commit:
        return Rejection(repo, "no commit hash")
    lic = _license_of(info)
    if lic not in policy.allowed_licenses:
        return Rejection(repo, f"license not approved: {lic}")

    siblings = info.get("siblings") or []
    names = [s.get("rfilename", "") for s in siblings]
    if any(n.endswith(".py") for n in names):
        return Rejection(repo, "contains custom code")

    files: dict[str, str] = {}
    total = 0
    for s in siblings:
        name = s.get("rfilename", "")
        if not name.endswith(WEIGHT_SUFFIX):
            continue
        lfs = s.get("lfs") or {}
        sha = lfs.get("sha256")
        if not sha:
            return Rejection(repo, f"no sha256 for {name}")
        files[name] = sha
        total += int(lfs.get("size") or s.get("size") or 0)
    if not files:
        return Rejection(repo, "no safetensors weights")
    if total > policy.max_weight_bytes:
        return Rejection(repo, f"too large: {total} bytes")

    return Candidate(repo=repo, commit=commit, license=lic, weight_bytes=total,
                     weights_digest=weights_digest(files), files=files)


def http_get_json(url: str, timeout: float = 30) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


class Scout:
    def __init__(self, policy: Policy = Policy(),
                 get_json: Callable[[str], object] = http_get_json,
                 endpoint: str = HF_ENDPOINT):
        self.policy = policy
        self.get_json = get_json
        self.endpoint = endpoint.rstrip("/")

    def recent_repos(self, limit: int = 50) -> list[str]:
        q = urllib.parse.urlencode([
            ("filter", "safetensors"), ("filter", self.policy.pipeline_tag),
            ("sort", "lastModified"), ("direction", "-1"), ("limit", str(limit)),
        ])
        items = self.get_json(f"{self.endpoint}/api/models?{q}")
        return [it.get("id") or it.get("modelId") for it in items]  # type: ignore[union-attr]

    def inspect(self, repo: str, revision: str = "main") -> Candidate | Rejection:
        url = (f"{self.endpoint}/api/models/{urllib.parse.quote(repo, safe='/')}"
               f"/revision/{urllib.parse.quote(revision, safe='')}?blobs=true")
        try:
            info = self.get_json(url)
        except Exception as e:  # network or HTTP error: skip this repo, keep scanning
            return Rejection(repo, f"fetch failed: {e}")
        return evaluate(info, self.policy)  # type: ignore[arg-type]

    def scan(self, repos: Iterable[str] | None = None, limit: int = 50,
             known_digests: set[str] = frozenset()) -> tuple[list[Candidate], list[Rejection]]:
        cands, rejects = [], []
        for repo in (repos if repos is not None else self.recent_repos(limit)):
            r = self.inspect(repo)
            if isinstance(r, Candidate):
                if r.weights_digest in known_digests:
                    rejects.append(Rejection(repo, "already archived"))
                else:
                    cands.append(r)
            else:
                rejects.append(r)
        return cands, rejects
