from cgai.hashing import weights_digest
from cgai.scout import Candidate, Policy, Rejection, Scout, evaluate

SHA = "ab" * 32


def info(**over):
    base = {
        "id": "org/model", "sha": "c" * 40, "private": False, "gated": False,
        "disabled": False, "tags": ["license:apache-2.0"], "cardData": {"license": "apache-2.0"},
        "siblings": [
            {"rfilename": "config.json", "size": 800},
            {"rfilename": "model.safetensors", "size": 1000, "lfs": {"sha256": SHA, "size": 1000}},
            {"rfilename": "training_args.bin", "size": 10, "lfs": {"sha256": "ff" * 32}},
        ],
    }
    base.update(over)
    return base


def test_accepts_open_safetensors_model_pinned_to_commit():
    c = evaluate(info())
    assert isinstance(c, Candidate)
    assert c.commit == "c" * 40
    assert c.files == {"model.safetensors": SHA}  # pickled files are never part of the model
    assert c.weights_digest == weights_digest({"model.safetensors": SHA})


def test_license_falls_back_to_tags():
    assert isinstance(evaluate(info(cardData=None)), Candidate)


def reason(i, policy=Policy()):
    r = evaluate(i, policy)
    assert isinstance(r, Rejection)
    return r.reason


def test_rejections():
    assert "license" in reason(info(cardData={"license": "llama3"}, tags=[]))
    assert "gated" in reason(info(gated="auto"))
    assert "private" in reason(info(private=True))
    assert "commit" in reason(info(sha=None))
    assert "safetensors" in reason(info(siblings=[{"rfilename": "pytorch_model.bin"}]))
    assert "custom code" in reason(info(siblings=info()["siblings"] + [{"rfilename": "modeling_x.py"}]))
    assert "too large" in reason(info(), Policy(max_weight_bytes=999))
    assert "sha256" in reason(info(siblings=[{"rfilename": "model.safetensors", "size": 5}]))


def test_scan_skips_archived_and_survives_fetch_errors():
    good = info()
    digest = evaluate(good).weights_digest

    def get_json(url):
        if "/api/models?" in url:
            return [{"id": "org/model"}, {"id": "org/broken"}, {"id": "org/new"}]
        if "org/broken" in url:
            raise OSError("boom")
        if "org/new" in url:
            return info(id="org/new", siblings=[{"rfilename": "model.safetensors",
                                                 "lfs": {"sha256": "cd" * 32, "size": 5}}])
        return good

    cands, rejects = Scout(get_json=get_json).scan(known_digests={digest})
    assert [c.repo for c in cands] == ["org/new"]
    assert {r.repo: r.reason.split(":")[0] for r in rejects} == {
        "org/model": "already archived", "org/broken": "fetch failed"}
