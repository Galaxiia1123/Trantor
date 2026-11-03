import json
import threading
import time
import uuid
from collections import Counter

import pytest
from fastapi.testclient import TestClient

from chuantuo.demo import demo_config, demo_scenario, respond
from chuantuo.engine import Simulation
from chuantuo.insights import Question, actor_observation, answer_question, timeline
from chuantuo.jobs import Jobs
from chuantuo.models import ModelError
from chuantuo.repository import Repository
from chuantuo.services import generate_report
from chuantuo.web import create_app


def poll(client, path, terminal=("completed", "failed", "interrupted")):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        reply = client.get(path)
        assert reply.is_success, reply.text
        data = reply.json()
        if data["status"] in terminal:
            return data
        time.sleep(.01)
    pytest.fail("background task did not finish")


def test_project_versions_reopen_and_launch_receipt(tmp_path):
    job = uuid.uuid4().hex
    with TestClient(create_app(tmp_path)) as client:
        bundle = client.get("/api/demo").json()
        bundle["config"]["windows"] = 1
        original = client.post("/api/projects", json=bundle).json()
        pid = original["id"]
        bundle["scenario"]["title"] += "（修订）"
        updated = client.post("/api/projects/" + pid, json={**bundle, "expected_version":1})
        assert updated.json()["version"] == 2
        assert client.post("/api/projects/" + pid, json={**bundle, "expected_version":1}).status_code == 409
        assert client.get("/api/projects/" + pid + "?version=1").json() == original
        bundle.update(project_id=pid, project_version=2)
        launch = "/api/jobs/" + job + "/launch"
        assert client.post(launch, json=bundle).status_code == 200
        result = poll(client, "/api/jobs/" + job)
        rid = result["result"]["run_id"]
        assert client.post(launch, json=bundle).json()["result"]["run_id"] == rid
        assert poll(client, "/api/runs/" + rid)["status"] == "completed"
        report = client.post("/api/runs/" + rid + "/report").json()
        file = tmp_path / rid / "events.jsonl"
        baseline = file.read_bytes()
    with TestClient(create_app(tmp_path)) as new:
        path = "/api/runs/" + rid
        assert new.get("/api/runs").json()["runs"][0]["project_id"] == pid
        assert new.get(path).json()["window"] == 1
        assert new.get(path + "/reports/" + report["record_id"]).json()["markdown"] == report["markdown"]
        assert len(new.get(path + "/events?after=0&limit=4&preview=true").json()["events"]) == 4
        full = new.get(path + "/events/e000001").json()
        assert full["data"]["scenario"]["title"].endswith("（修订）")
        assert new.get(path + "/events/e000000").status_code == 404
        assert new.post(launch, json=bundle).json()["result"]["run_id"] == rid
        assert file.read_bytes() == baseline


def test_restart_restores_exact_private_memory_inbox_and_state(tmp_path):
    class PauseAtOne:
        def complete(self, model, messages):
            payload = json.loads(messages[-1]["content"])
            if payload["task"] == "world" and payload["window"] == 1:
                sim.request_pause()
            return respond(messages)

    config = demo_config().model_copy(update={"windows":2})
    sim = Simulation(demo_scenario(), config, PauseAtOne(), tmp_path)
    assert sim.run()["status"] == "paused"
    baseline = (tmp_path / "events.jsonl").read_bytes()
    restored = Simulation.restore(tmp_path)
    assert (tmp_path / "events.jsonl").read_bytes() == baseline
    assert restored.state == sim.state and restored._inbox == sim._inbox
    for key in sim.agents:
        assert restored.agents[key].get_state() == sim.agents[key].get_state()
    assert restored.run()["status"] == "completed"
    clean = Simulation(demo_scenario(), config)
    assert clean.run()["state"] == restored.state
    commits = restored.journal.snapshot({"window_committed"})
    assert [e["window"] for e in commits] == [1,2]
    views = [e["data"]["visible"] for e in restored.journal.snapshot({"observation"})
             if e["window"] == 2 and e["data"]["actor_id"] == "city"]
    assert "PRIVATE_PORT" not in json.dumps(views)
    assert any(m["from"] == "engineer" for m in views[0]["messages"])


def test_failed_window_recovery_excludes_abandoned_records_even_after_restart(tmp_path):
    marker = "AbandonedAttempt"
    class FailedWorld:
        def complete(self, model, messages):
            data = json.loads(messages[-1]["content"])
            if data["task"] == "world" and data["window"] == 2:
                raise ModelError("test interruption")
            if data.get("actor_id") == "city" and data["window"] == 2:
                return json.dumps({"text":marker})
            return respond(messages)

    sim = Simulation(demo_scenario(), demo_config(), FailedWorld(), tmp_path)
    assert sim.run()["status"] == "failed"
    assert timeline(sim)["statistics"]["expressions"] == 10
    assert actor_observation(sim, "city", 2)["observation"] is None
    restored = Simulation.restore(tmp_path)
    restored.recover()
    assert restored.run()["status"] == "completed"
    audit = restored.journal.snapshot()
    discarded = set(next(e for e in audit if e["kind"] == "run_recovered")["data"]["discarded_event_ids"])
    assert {e["id"] for e in audit if e["kind"] == "expression" and e["data"]["text"] == marker} <= discarded
    assert timeline(restored)["statistics"]["expressions"] == 30
    again = Simulation.restore(tmp_path)
    assert marker not in json.dumps(again.agents["city"].get_state())
    assert marker not in json.dumps(answer_question(again, Question(text="你做过什么？", actor_id="city")))
    assert [e["window"] for e in again.journal.snapshot({"window_committed"})] == [1,2,3]


def test_questions_cannot_cite_unseen_events_or_affect_memory(tmp_path):
    class BadAnswer:
        attempts = 0
        def complete(self, model, messages):
            data = json.loads(messages[-1]["content"])
            if data["task"] == "analysis":
                self.attempts += 1
                assert "PRIVATE_PORT" not in messages[-1]["content"]
                return json.dumps({"text":"unsupported", "evidence_ids":["e000001"]})
            return respond(messages)
    gateway = BadAnswer()
    sim = Simulation(demo_scenario(), demo_config().model_copy(update={"windows":1}), gateway, tmp_path)
    sim.run()
    memory = sim.agents["city"].get_state()
    with pytest.raises(ValueError, match="引用"):
        answer_question(sim, Question(text="你知道什么？", actor_id="city"))
    assert gateway.attempts == 3 and not sim.journal.snapshot({"analysis_answer"})
    assert sim.agents["city"].get_state() == memory


def test_interview_uses_the_deciding_rounds_input_and_no_later_delivery(tmp_path):
    class Gateway:
        def complete(self, model, messages):
            payload=json.loads(messages[-1]["content"])
            if payload["task"]=="agent" and payload["actor_id"]=="engineer" and payload["round"]==1:
                return json.dumps({"text":"private-sender-choice","messages":[{"recipients":["city"],"text":"receipt-R2"}]})
            if payload["task"]=="analysis":
                assert ("receipt-R2" in messages[-1]["content"]) == (payload["cutoff_round"]==2)
                assert "private-sender-choice" not in messages[-1]["content"]
            return respond(messages)
    sim=Simulation(demo_scenario(),demo_config().model_copy(update={"windows":1}),Gateway(),tmp_path)
    assert sim.run()["status"]=="completed"
    memory=sim.agents["city"].get_state()
    actions=sim.journal.snapshot({"expression"})
    for action in (a for a in actions if a["data"]["actor_id"]=="city"):
        question=Question(text="Which messages had arrived?",actor_id="city",anchor=action["id"],max_calls=1)
        answer=answer_question(sim,question)
        assert answer["cutoff_round"]==action["data"]["round"] and answer["anchor_record"]==action["id"]
        assert {e["id"] for e in answer["evidence"]}=={answer["context_record"],action["id"]}
    with pytest.raises(ValueError,match="当事|主体"):
        answer_question(sim,Question(text="收到过吗？",actor_id="city",anchor=next(a["id"] for a in actions if a["data"]["actor_id"]=="port")))
    assert Simulation.restore(tmp_path).agents["city"].get_state()==memory


def test_corrupt_or_stale_log_never_overwrites_evidence(tmp_path):
    directory = tmp_path / "old-folder"
    sim = Simulation(demo_scenario(), demo_config(), directory=directory)
    second = Simulation.restore(directory)
    sim.journal.append("test", {"text": "Unicode separator \u2028 preserved"})
    with pytest.raises(ValueError, match="另一进程"):
        second.journal.append("test", {"text": "stale"})
    assert Simulation.restore(directory).journal.event("e000002")["data"]["text"].endswith("preserved")
    path = directory / "events.jsonl"
    with path.open("ab") as stream:
        stream.write(b'{"id":')
    corrupt = path.read_bytes()
    assert Repository(tmp_path).catalog()["warnings"]
    with pytest.raises(ValueError, match="不完整"):
        Simulation.restore(directory)
    assert path.read_bytes() == corrupt


def test_definition_background_job_deduplicates_concurrent_submissions(tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Gateway:
        calls = Counter()
        def complete(self, config, messages):
            data = json.loads(messages[-1]["content"])
            self.calls[data["task"]] += 1
            if data["task"] == "define":
                entered.set()
                assert release.wait(5)
            return respond(messages)
    gateway = Gateway()
    job_id = uuid.uuid4().hex
    path = "/api/jobs/" + job_id
    with TestClient(create_app(tmp_path, gateway)) as client:
        model = {"provider":"demo", "model":"fixture"}
        body = {"goal":"test", "model":model, "world_model":model}
        client.post(path + "/definition", json=body).raise_for_status()
        assert entered.wait(5)
        try:
            assert client.post(path + "/definition", json=body).json()["status"] == "running"
            assert client.post(path + "/definition", json={**body,"goal":"different"}).status_code == 409
        finally:
            release.set()
        assert poll(client, path)["result"]["requires_review"]
        assert gateway.calls["define"] == 1
        assert not client.get("/api/runs").json()["runs"]
    jobs = Jobs(tmp_path / "_jobs")
    assert jobs.get(job_id)["status"] == "completed"


def test_model_call_limit_counts_attempts_across_concurrency_and_reload(tmp_path):
    config = demo_config().model_copy(update={"model_call_limit":6})
    sim = Simulation(demo_scenario(), config, directory=tmp_path)
    assert sim.run()["status"] == "failed" and sim.window == 0
    assert len(sim.journal.snapshot({"model_input"})) == 6
    restored = Simulation.restore(tmp_path)
    assert restored.snapshot()["model_calls"] == 6 and not restored.snapshot()["can_resume"]


def test_long_report_coverage_and_private_memory_fit(tmp_path):
    letters = "证据PrivateLongText／" * 900
    original = demo_scenario().model_dump(mode="json")
    original["sources"] = [{"id":"long", "name":"长材料", "text":letters}]
    from chuantuo.schema import Scenario, RunConfig
    config = demo_config().model_dump(mode="json")
    config["windows"], config["memory_policy"] = 4, "budgeted"
    config["models"]["configs"]["small"] = {"provider":"demo","model":"small","context_limit_chars":6200}
    config["models"]["report"] = "small"
    config["models"]["layers"] = {0:"small",1:"small",2:"small"}
    sim = Simulation(Scenario.model_validate(original), RunConfig.model_validate(config), directory=tmp_path)
    assert sim.run()["status"] == "completed"
    for event in sim.journal.snapshot({"model_input"}):
        if event["data"]["purpose"] != "agent":
            continue
        messages = event["data"]["messages"]
        assert sum(len(m["content"]) for m in messages) <= 6200
        prompt = json.loads(messages[-1]["content"])
        if prompt["window"] == 4:
            assert prompt["memory_coverage"]["retained_items"] < prompt["memory_coverage"]["total_items"]
        if prompt["actor_id"] != "port":
            assert "PRIVATE_PORT" not in messages[-1]["content"]
    result = generate_report(sim)
    coverage = result["coverage"]
    assert coverage["method"] == "batched" and coverage["batches"] > 1
    ids = set()
    source_fragments = []
    for event in sim.journal.snapshot({"report_batch_input"}):
        data = event["data"]
        assert sum(len(m["content"]) for m in data["messages"]) <= 6200
        if data["level"] == 1:
            for part in json.loads(data["messages"][-1]["content"])["items"]:
                ids.update(part["evidence_ids"])
                if "e000001" in part["evidence_ids"]:
                    source_fragments.append(part["text"])
    assert len(ids) == coverage["evidence_count"]
    assert json.loads("".join(source_fragments))["scenario"]["sources"][0]["text"] == letters
    assert all(set(section["evidence_ids"]) <= ids for section in result["report"]["sections"])
