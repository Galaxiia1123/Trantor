import json

import pytest

from chuantuo.engine import Simulation, encode
from chuantuo.experiments import ExperimentRequest, fork_experiment
from chuantuo.first_round import FILENAME
from chuantuo.models import ModelConfig, ModelError
from chuantuo.schema import RunConfig11, Scenario11
from chuantuo.studies import StudyRequest


class Gateway:
    def __init__(self):
        self.requests = []
        self.fail_world = False

    def complete(self, config, messages):
        data = json.loads(messages[-1]["content"])
        self.requests.append(data)
        if data["task"] == "agent":
            return json.dumps({"text": "自主判断 " + data["actor_id"], "messages": []})
        if self.fail_world:
            self.fail_world = False
            raise ModelError("fixture world interruption")
        return '{"reviews":[],"consequences":[]}'


def parent_run(path, gateway):
    scene = Scenario11(title="相同信息", background="两位参与者各自判断。", domains=["观察"],
                       entities=[{"id": "a", "name": "甲", "identity": "甲的稳定档案。" * 900},
                                 {"id": "b", "name": "乙", "identity": "乙的独立档案"}])
    config = RunConfig11(model=ModelConfig(provider="custom", model="fixture", base_url="http://127.0.0.1:9320"),
                        windows=2, rounds_per_window=1)
    parent = Simulation(scene, config, gateway, path)
    assert parent.run()["status"] == "completed"
    gateway.requests.clear()
    return parent


def test_first_round_choices_are_counted_separately_and_survive_recovery(tmp_path):
    gateway = Gateway()
    parent = parent_run(tmp_path / "parent", gateway)
    original = (parent.journal.directory / "events.jsonl").read_bytes()
    spec = ExperimentRequest(window=0, through=2, condition="world-only-marker", reuse_first_round=True)
    child = fork_experiment(parent, spec, "a" * 12, None, tmp_path / "child")
    gateway.fail_world = True
    assert child.run()["status"] == "failed" and child.window == 0
    assert [r["task"] for r in gateway.requests] == ["world"]
    assert child.snapshot()["model_calls"] == 1
    rows = [json.loads(line) for line in (child.journal.directory / "events.jsonl").read_text("utf-8").splitlines()]
    assert any(row["kind"] == "model_input" and "_packed" in row and row["data"].get("reused_from") for row in rows)
    # The already adopted choices contain their own input and output records.
    (child.journal.directory / FILENAME).unlink()
    restored = Simulation.restore(child.journal.directory, gateway)
    assert restored.snapshot()["model_calls"] == 1
    restored.recover()
    assert restored.run()["status"] == "completed" and restored.snapshot()["model_calls"] == 5
    usage = restored.journal.usage()
    assert usage["主体"]["calls"] == 2 and usage["主体"]["local_replays"] == 2
    assert usage["世界"]["calls"] == 3
    context = restored.journal.decision_context("b", 1, 1, restored._discarded)
    assert context["reused_from"]["run_id"] == parent.run_id
    assert context["reused_from"]["actor_id"] == "b"
    assert "world-only-marker" not in encode(context["observation"])
    assert (parent.journal.directory / "events.jsonl").read_bytes() == original


def test_changed_information_is_decided_afresh_and_reuse_requires_an_explicit_choice(tmp_path):
    gateway = Gateway()
    parent = parent_run(tmp_path / "parent", gateway)
    spec = ExperimentRequest(window=0, through=2, condition="only-a-update", audience="readers", readers=["a"],
                             reuse_first_round=True)
    child = fork_experiment(parent, spec, "b" * 12, None, tmp_path / "changed")
    assert child.run()["status"] == "completed"
    first = [r["actor_id"] for r in gateway.requests if r["task"] == "agent" and r["window"] == 1]
    assert first == ["a"]
    context = child.journal.decision_context("b", 1, 1, child._discarded)
    assert context["reused_from"]["actor_id"] == "b" and "only-a-update" not in encode(context)
    assert child.journal.usage()["主体"]["local_replays"] == 1
    original_spec = ExperimentRequest(window=0, through=2, condition="world-only-marker")
    assert "reuse_first_round" not in original_spec.model_dump(mode="json")
    with pytest.raises(ValueError, match="独立判断"):
        StudyRequest(experiment=spec, repetitions=2, call_limit=12)
    with pytest.raises(ValueError, match="单条条件对照"):
        ExperimentRequest(window=0, through=2, kind="repeat", reuse_first_round=True)
    gateway.requests.clear()
    broken = fork_experiment(parent, spec, "c" * 12, None, tmp_path / "broken")
    (broken.journal.directory / FILENAME).write_text("damaged fixture", encoding="utf-8")
    assert broken.run()["status"] == "failed" and not gateway.requests
    assert broken.snapshot()["model_calls"] == 0 and "损坏" in broken.error
