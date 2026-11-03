import json
import threading
from concurrent.futures import ThreadPoolExecutor

from chuantuo.bridge import resolve
from chuantuo.demo import demo_config, demo_scenario, respond
from chuantuo.engine import Simulation
from chuantuo.schema import Scenario
from chuantuo.services import estimate_weights, generate_report


def test_estimated_private_reason_is_not_delivered_to_other_agents():
    reason = "PRIVATE_PORT: reserve equipment makes the port more influential."

    class RecordingGateway:
        def __init__(self):
            self.inputs = []

        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            self.inputs.append(payload)
            if payload["task"] == "estimate_weights":
                return json.dumps({"estimates": [{"source": "port", "target": "city",
                    "weight": 0.7, "reason": reason}]})
            return respond(messages)

    data = demo_scenario().model_dump()
    data["influences"][-1]["weight"] = None
    gateway = RecordingGateway()
    config = demo_config().model_copy(update={"windows": 1, "rounds_per_window": 1})
    estimated = estimate_weights(Scenario.model_validate(data), config.models.configs["offline"], gateway)
    sim = Simulation(Scenario.model_validate(estimated["scenario"]), config, gateway)
    sim.journal.append("weight_estimation", estimated["record"])
    assert sim.run()["status"] == "completed"

    agents = [payload for payload in gateway.inputs if payload["task"] == "agent"]
    assert all("PRIVATE_PORT" not in json.dumps(payload) for payload in agents if payload["actor_id"] != "port")
    for payload in agents:
        for edge in payload["observation"]["influences"]:
            assert {"source", "target", "weight"} <= edge.keys()
            assert edge.keys() <= {"source", "target", "weight", "weight_source"}
    city = next(payload for payload in agents if payload["actor_id"] == "city")
    assert any(edge["source"] == "port" and edge["weight"] == 0.7
               for edge in city["observation"]["influences"])
    world = next(payload for payload in gateway.inputs if payload["task"] == "world")
    assert world["scenario"]["influences"][-1]["provenance"]["explanation"] == reason
    audit = sim.journal.snapshot()
    assert audit[0]["data"]["scenario"]["influences"][-1]["provenance"]["explanation"] == reason
    assert next(event for event in audit if event["kind"] == "weight_estimation")["data"]["estimates"][0]["reason"] == reason


def test_world_history_preserves_fieldless_consequences_and_excludes_rejected_proposals():
    committed_fact = "The north bridge is closed until repairs finish."
    rejected_fact = "REJECTED: the north bridge reopened."

    class HistoryGateway:
        def __init__(self):
            self.world_inputs = []

        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            if payload["task"] == "agent":
                return json.dumps({"text": "I will inspect the south station.", "messages": []})
            self.world_inputs.append(payload)
            first = payload["window"] == 1
            return json.dumps({"reviews": [], "consequences": [{
                "text": committed_fact if first else rejected_fact,
                "action_ids": [payload["actions"][0]["id"]] if first else ["e999999"],
            }]})

    scenario = Scenario(title="Fieldless history", background="Two crossings.", domains=[],
                        entities=[{"id": "inspector", "name": "Inspector", "identity": "Independent inspector"}])
    gateway = HistoryGateway()
    config = demo_config().model_copy(update={"windows": 2, "rounds_per_window": 1})
    sim = Simulation(scenario, config, gateway)
    assert sim.run()["status"] == "failed"
    assert sim.window == 1
    assert sim.state == {"inspector": {}}
    committed = next(event for event in sim.journal.snapshot() if event["kind"] == "window_committed")
    expected_history = [{"id": committed["id"], "window": 1,
                         "consequences": committed["data"]["consequences"]}]
    assert gateway.world_inputs[1].get("history") == expected_history
    assert gateway.world_inputs[0].get("history") == []
    assert committed_fact not in json.dumps(gateway.world_inputs[1]["actions"])

    # Ask the existing world agent again so its actual memory includes the
    # rejected output. Only committed facts may enter the next world input.
    resolve(sim.world)
    assert gateway.world_inputs[-1]["history"] == expected_history
    assert rejected_fact not in json.dumps(gateway.world_inputs[-1])
    assert any(rejected_fact in event["data"].get("text", "")
               for event in sim.journal.snapshot() if event["kind"] == "model_output")


def test_report_uses_atomic_paused_snapshot_while_resume_can_finish_during_model_call(monkeypatch):
    class PauseOnceGateway:
        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            if payload["task"] == "world" and payload["window"] == 1:
                sim.request_pause()
            return respond(messages)

    config = demo_config().model_copy(update={"windows": 2, "rounds_per_window": 1})
    sim = Simulation(demo_scenario(), config, PauseOnceGateway())
    paused = sim.run()
    assert paused["status"] == "paused" and paused["window"] == 1
    original_snapshot = sim.journal.snapshot
    original_selected = sim.journal.selected
    report_thread = threading.get_ident()
    probe_done = False
    resumed = None
    report_payload = None

    def try_resume_at_capture():
        # Force the old events/state race without sleeps. A correct atomic
        # capture owns the same lock and defers resume until the model call.
        if not sim._lock.acquire(blocking=False):
            return None
        try:
            return sim.run()
        finally:
            sim._lock.release()

    with ThreadPoolExecutor(max_workers=1) as executor:
        def snapshot_with_competing_resume(*args, **kwargs):
            nonlocal probe_done, resumed
            events = original_selected(*args, **kwargs)
            if threading.get_ident() == report_thread and not probe_done:
                probe_done = True
                resumed = executor.submit(try_resume_at_capture).result(timeout=10)
            return events

        class ConcurrentReportGateway:
            def complete(self, model, messages):
                nonlocal resumed, report_payload
                report_payload = json.loads(messages[-1]["content"])
                if resumed is None:
                    # Waiting here also proves the report does not retain the
                    # simulation lock throughout a potentially slow HTTP call.
                    resumed = executor.submit(sim.run).result(timeout=10)
                return respond(messages)

        monkeypatch.setattr(sim.journal, "selected", snapshot_with_competing_resume)
        result = generate_report(sim, ConcurrentReportGateway())

    assert probe_done and resumed["status"] == "completed" and resumed["window"] == 2
    assert report_payload["status"] == paused
    commits = [event for event in report_payload["events"] if event["kind"] == "window_committed"]
    assert [event["window"] for event in commits] == [1]
    assert commits[-1]["data"]["state"] == report_payload["status"]["state"]
    assert result["cutoff_window"] == 1
    report_events = [event for event in original_snapshot()
                     if event["kind"] in {"report_input", "report_output", "report_created"}]
    assert [event["kind"] for event in report_events] == ["report_input", "report_output", "report_created"]
    assert all(event["window"] == 1 for event in report_events)
