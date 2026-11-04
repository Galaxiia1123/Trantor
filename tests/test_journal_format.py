import base64
import json
import zlib

import pytest
from fastapi.testclient import TestClient

from chuantuo.demo import demo_scenario
from chuantuo.engine import Journal, Simulation
from chuantuo.experiments import ExperimentRequest, fork_experiment
from chuantuo.journal_format import read_record, record_bytes
from chuantuo.insights import event_preview
from chuantuo.schema import RunConfig11, Scenario11
from chuantuo.web import create_app


def test_packed_transport_restores_exact_records_and_plain_http_export(tmp_path):
    directory = tmp_path / "0123456789ab"
    scene = Scenario11.model_validate({**demo_scenario().model_dump(mode="json"),
                                       "format_version":"1.1", "influences":[]})
    config = RunConfig11.model_validate({"model":{"provider":"demo", "model":"scripted-demo",
                                                 "context_limit_chars":1000000},
                                        "windows":2, "rounds_per_window":2})
    sim = Simulation(scene, config, directory=directory, _run_id=directory.name)
    class FirstBoundary:
        def is_set(self):
            return sim.window >= 1
    assert sim.run(pause_at_boundary=FirstBoundary())["status"] == "paused"
    note = sim.journal.append("report_input", {"config_id":"shared", "messages":[
        {"role":"user", "content":"完整原文 " * 3000}]}, 1)
    sim.run()
    before = sim.journal.snapshot()
    lines = (directory / "events.jsonl").read_bytes().splitlines(keepends=True)
    assert any("_packed" in json.loads(line) for line in lines)
    assert len(b"".join(lines)) < len(b"".join(sim.journal.export_lines())) * 0.8
    restored = Simulation.restore(directory)
    assert restored.state == sim.state and restored.journal.snapshot() == before
    assert restored.journal.usage() == sim.journal.usage()
    for actor in sim.agents:
        assert restored.journal.decision_context(actor, 2, 1, set()) == sim.journal.decision_context(actor, 2, 1, set())
    # Existing uncompressed logs and newly packed appends can coexist.
    plain = tmp_path / "legacy"
    plain.mkdir()
    (plain / "events.jsonl").write_bytes(b"".join(sim.journal.export_lines()))
    legacy = Journal.open(plain)
    large = {"purpose":"agent", "actor_id":"city", "text":"保留原文与引号 «场景»\n" * 2000}
    extra = legacy.append("model_output", large, 3)
    assert Journal.open(plain).event(extra["id"])["data"] == large
    with TestClient(create_app(tmp_path)) as client:
        response = client.get(f"/api/runs/{directory.name}/export")
        assert response.status_code == 200
        exported = [json.loads(line) for line in response.content.splitlines()]
        assert exported == before and all("_packed" not in event for event in exported)
    branch = fork_experiment(restored, ExperimentRequest(kind="extend", window=2, through=3, extension_limit=12),
                             "b"*12, None, tmp_path / ("b"*12))
    assert branch.window == 2 and branch.state == sim.state
    assert branch.journal.event(note["id"])["kind"] == "inherited_reference"
    assert "_packed" not in branch.journal.event(note["id"])


def test_corrupt_packed_records_stop_without_fabricating_text(tmp_path):
    journal = Journal(tmp_path)
    journal.append("run_created", {"test":True})
    original = journal.append("model_input", {"purpose":"agent", "actor_id":"alpha",
                              "messages":[{"role":"user", "content":"只属于甲的资料" * 3000}]})
    packed = json.loads(record_bytes(original))
    assert "_packed" in packed and read_record(json.dumps(packed)) == original
    for defect in ("digest", "header", "trailing", "size", "oversized", "codec"):
        damaged = json.loads(json.dumps(packed))
        body = damaged["_packed"]
        if defect == "digest":
            body["sha256"] = "0" * 64
        elif defect == "header":
            damaged["data"]["actor_id"] = "other"
        elif defect == "trailing":
            body["body"] = base64.b64encode(base64.b64decode(body["body"]) + zlib.compress(b"extra")).decode()
        elif defect == "size":
            body["bytes"] -= 1
        elif defect == "oversized":
            body["bytes"] = 2 ** 40
        else:
            body["codec"] = "unknown"
        with pytest.raises(ValueError, match="压缩"):
            read_record(json.dumps(damaged))
    lines = (tmp_path / "events.jsonl").read_bytes().splitlines(keepends=True)
    lines[-1] = json.dumps(damaged).encode() + b"\n"
    (tmp_path / "events.jsonl").write_bytes(b"".join(lines))
    with pytest.raises(ValueError, match="第 2 行"):
        Journal.open(tmp_path)


def test_old_observations_remain_complete_after_cache_eviction(tmp_path):
    journal = Journal(tmp_path)
    journal.append("run_created", {"test":True})
    for window in range(1,81):
        journal.append("observation", {"actor_id":"a", "visible":{"round":1, "visible_state":{"a":{"value":window}},
                       "visible_materials":[{"text":"专属材料"*2000}], "messages":[]}}, window)
    assert len(journal._observation_cache) <= 64 and journal._observation_bytes <= 4*1024*1024
    assert "visible" not in journal._events[1]["data"]
    oldest = journal.round_observation("a",1,1,set())
    oldest["data"]["visible"]["visible_state"]["a"]["value"] = 999
    assert journal.event("e000002")["data"]["visible"]["visible_state"]["a"]["value"] == 1
    preview = journal.page(1,1,event_preview)["events"][0]["data"]
    assert (preview["round"],preview["field_count"],preview["material_count"]) == (1,1,1)
    reopened = Journal.open(tmp_path)
    records = reopened.selected({"observation"},transport_details=False)
    assert len(records) == 80 and records[-1]["data"]["visible"]["visible_state"]["a"]["value"] == 80
    assert len(reopened._observation_cache) <= 64
