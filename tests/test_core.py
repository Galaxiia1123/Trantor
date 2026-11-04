import json
from collections import Counter

import pytest
from pydantic import ValidationError

from chuantuo.demo import demo_config, demo_scenario, respond
from chuantuo.engine import Simulation
from chuantuo.models import ModelError
from chuantuo.schema import Scenario, Settlement
from chuantuo.services import estimate_weights, generate_report, parse_scenario


def test_full_run_cross_layers_fair_windows_privacy_and_report(tmp_path):
    sim = Simulation(demo_scenario(), demo_config(), directory=tmp_path)
    result = sim.run()
    assert result["status"] == "completed"
    assert result["window"] == 3
    assert sim.state["city"] == {"readiness": 35}
    events = sim.journal.snapshot()
    actions = [e for e in events if e["kind"] == "expression"]
    assert Counter(e["data"]["actor_id"] for e in actions) == {a.id: 6 for a in sim.scenario.entities}
    assert len([e for e in events if e["kind"] == "window_committed"]) == 3
    observations = [e["data"] for e in events if e["kind"] == "observation"]
    for observation in observations:
        if observation["actor_id"] != "port":
            assert "PRIVATE_PORT" not in json.dumps(observation)
    for actor in ["city", "company", "community"]:
        second = next(o["visible"] for o in observations if o["actor_id"] == actor and o["visible"]["round"] == 2)
        assert any(m["from"] == "engineer" for m in second["messages"])
    assert sim.depths == {"city": 0, "port": 1, "engineer": 2, "community": 1, "company": 0}
    report = generate_report(sim)
    assert "离线流程验证" in report["markdown"]
    assert (tmp_path / f"report-{report['record_id']}.md").exists()
    assert not any("funds" in fields for fields in sim.state.values())
    assert (tmp_path / "events.jsonl").read_text(encoding="utf-8").count('"kind": "window_committed"') == 3


def test_invalid_json_is_corrected_for_only_the_affected_actor(tmp_path):
    class Correcting:
        counts = Counter()
        correction_seen = False

        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            key = (payload["task"], payload.get("actor_id"), payload.get("window"), payload.get("round"))
            self.counts[key] += 1
            if key == ("agent", "city", 1, 1):
                if self.counts[key] == 1:
                    return '{"text":"intent"},"messages":[]}'
                assert payload["output_correction"]["previous_response"] == '{"text":"intent"},"messages":[]}'
                self.correction_seen = True
            return respond(messages)

    gateway = Correcting()
    sim = Simulation(demo_scenario(), demo_config().model_copy(update={"windows": 1}), gateway, tmp_path)
    assert sim.run()["status"] == "completed"
    assert gateway.correction_seen
    assert gateway.counts[("agent", "city", 1, 1)] == 2
    assert all(count == 1 for key, count in gateway.counts.items() if key != ("agent", "city", 1, 1))
    events = sim.journal.snapshot()
    assert len([e for e in events if e["kind"] == "model_validation_error"]) == 1
    assert len([e for e in events if e["kind"] == "expression"]) == 10
    assert len([e for e in events if e["kind"] == "window_committed"]) == 1
    assert "intent" not in json.dumps(sim.agents["city"].get_state(), ensure_ascii=False)


def test_report_json_correction_preserves_evidence_snapshot(tmp_path):
    sim = Simulation(demo_scenario(), demo_config().model_copy(update={"windows": 1}), directory=tmp_path)
    sim.run()

    class CorrectingReport:
        calls = []

        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            self.calls.append(payload)
            if len(self.calls) == 1:
                return '{"title":"broken"},"sections":[]}'
            assert payload["output_correction"]["previous_response"] == '{"title":"broken"},"sections":[]}'
            return respond(messages)

    gateway = CorrectingReport()
    report = generate_report(sim, gateway)
    assert len(gateway.calls) == 2
    assert gateway.calls[0]["events"] == gateway.calls[1]["events"]
    assert gateway.calls[0]["status"] == gateway.calls[1]["status"]
    assert report["cutoff_window"] == 1


@pytest.mark.parametrize("defect", ["new_field", "missing_review", "duplicate_review", "wrong_before", "wrong_type", "bad_reference"])
def test_bad_world_proposal_leaves_entire_window_uncommitted(defect):
    class BadWorld:
        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            response = json.loads(respond(messages))
            if payload["task"] == "world":
                match defect:
                    case "new_field": response["reviews"][0]["field_id"] = "funds"
                    case "missing_review": response["reviews"].pop()
                    case "duplicate_review": response["reviews"].append(response["reviews"][0])
                    case "wrong_before": response["reviews"][0]["before"] = 999
                    case "wrong_type": response["reviews"][0]["after"] = True
                    case "bad_reference": response["reviews"][0]["action_ids"] = ["e999999"]
            return json.dumps(response)
    scenario = demo_scenario()
    sim = Simulation(scenario, demo_config(), BadWorld())
    assert sim.run()["status"] == "failed"
    assert sim.window == 0
    assert sim.state == scenario.initial_state()


def test_duplicate_commit_and_pause_resume_stop():
    sim = Simulation(demo_scenario(), demo_config())
    sim.request_pause()
    assert sim.run()["status"] == "paused"
    assert sim.window == 0
    assert sim.run()["status"] == "completed"
    with pytest.raises(ValueError, match="已经结算"):
        sim.commit(3, Settlement(reviews=[]), set())
    other = Simulation(demo_scenario(), demo_config())
    other.request_stop()
    assert other.status == "stopped"
    assert other.window == 0


def test_pause_requested_during_window_applies_at_boundary():
    class Pausing:
        sim = None
        def complete(self, config, messages):
            if json.loads(messages[-1]["content"])["task"] == "world":
                self.sim.request_pause()
            return respond(messages)
    gateway = Pausing()
    gateway.sim = Simulation(demo_scenario(), demo_config(), gateway)
    assert gateway.sim.run()["status"] == "paused"
    assert gateway.sim.window == 1
    gateway.sim.request_stop()
    assert gateway.sim.status == "stopped"


def test_failed_actor_no_partial_world_call_or_automatic_retry():
    class Failure:
        calls = []
        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            self.calls.append(payload["task"])
            if payload.get("actor_id") == "engineer":
                raise ModelError("selected model unavailable")
            return respond(messages)
    gateway = Failure()
    sim = Simulation(demo_scenario(), demo_config(), gateway)
    assert sim.run()["status"] == "failed"
    assert "world" not in gateway.calls
    assert len(gateway.calls) == 5
    assert sim.window == 0


def test_containment_cycles_rejected_influence_cycles_allowed():
    data = demo_scenario().model_dump()
    data["entities"][0]["subsystem_id"] = "port_team"
    with pytest.raises(ValidationError, match="包含结构不能循环"):
        Scenario.model_validate(data)
    scenario = demo_scenario()
    assert sum(e.weight for e in scenario.influences if e.target == "city") == pytest.approx(1.5)
    assert scenario.depths()["engineer"] == 2


def test_draft_preserves_input_and_weights_only_fill_missing():
    config = demo_config().models.configs["offline"]
    text = "我的原始材料：保留原文。"
    draft = parse_scenario(text, config)
    assert draft["requires_review"]
    assert draft["scenario"]["sources"][0]["text"] == text
    assert all(e["provenance"]["kind"] == "ai_assumption" for e in draft["scenario"]["entities"])
    data = demo_scenario().model_dump()
    data["influences"][0]["weight"] = None
    scenario = Scenario.model_validate(data)
    with pytest.raises(ValueError, match="未赋权"):
        Simulation(scenario, demo_config())
    estimated = Scenario.model_validate(estimate_weights(scenario, config)["scenario"])
    assert estimated.influences[0].weight == 0.5
    assert estimated.influences[0].provenance.kind == "ai_assumption"
    assert estimated.influences[1:] == scenario.influences[1:]


def test_snapshot_and_supplied_scenario_cannot_mutate_running_configuration():
    scenario = demo_scenario()
    sim = Simulation(scenario, demo_config())
    scenario.entities.clear()
    exposed = sim.scenario
    exposed.influences.clear()
    state = sim.state
    state["city"]["readiness"] = 999
    assert len(sim.scenario.entities) == 5
    assert len(sim.scenario.influences) == 5
    assert sim.state["city"]["readiness"] == 20


def test_report_rejects_fabricated_evidence():
    class Fabricated:
        def complete(self, config, messages):
            return '{"title":"无证据","sections":[{"title":"结果","text":"虚构","evidence_ids":["e999999"]}]}'
    sim = Simulation(demo_scenario(), demo_config())
    sim.run()
    with pytest.raises(ValueError, match="不存在的证据"):
        generate_report(sim, Fabricated())
