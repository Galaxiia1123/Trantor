import io
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from zipfile import ZipFile

import httpx
import pytest
from fastapi.testclient import TestClient

from chuantuo.demo import demo_config, demo_scenario, respond
from chuantuo.engine import Simulation
from chuantuo.intake import MaterialStore
from chuantuo.models import ModelGateway
from chuantuo.recovery import salvage
from chuantuo.repository import Repository
from chuantuo.schema import RunConfig
from chuantuo.web import create_app


@pytest.mark.parametrize("final_valid",[False,True])
def test_salvage_never_rewrites_source_and_is_idempotent(tmp_path,final_valid):
    src = tmp_path/"legacy"
    sim = Simulation(demo_scenario(),demo_config().model_copy(update={"windows":1}),directory=src)
    sim.run()
    path = src/"events.jsonl"
    raw = path.read_bytes()
    incomplete = raw.rstrip(b"\n") if final_valid else raw + b'{"id":"e0'
    path.write_bytes(incomplete)
    repo = Repository(tmp_path)
    assert repo.catalog()["warnings"][0]["can_salvage"]
    result=salvage(repo,src.name)
    assert result["kept_final_record"] is final_valid and result["source_run_id"]==sim.run_id
    assert result["discarded_tail_bytes"] == (0 if final_valid else len(incomplete)-len(raw))
    new = Simulation.restore(tmp_path/result["run_id"])
    assert new.run_id!=sim.run_id and new.state==sim.state and new.window==1
    assert salvage(repo,src.name)==result and path.read_bytes()==incomplete
    assert len(repo.catalog()["runs"])==1


def test_salvage_interior_corruption_fails_without_publishing(tmp_path):
    src=tmp_path/"broken"
    sim=Simulation(demo_scenario(),demo_config(),directory=src)
    path=src/"events.jsonl"
    with path.open("ab") as f:
        f.write(b'broken\n{"id":')
    repo=Repository(tmp_path)
    with pytest.raises(ValueError,match="损坏"):
        salvage(repo,"broken")
    assert not repo.catalog()["runs"] and sim.window==0


def test_docx_paragraph_and_table_text_preserve_original_without_invented_pages(tmp_path):
    doc=io.BytesIO()
    with ZipFile(doc,"w") as archive:
        archive.writestr("word/document.xml",'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>试验</w:t></w:r><w:r><w:t>目标</w:t></w:r></w:p><w:tbl><w:tr><w:tc><w:p><w:r><w:t>未知</w:t></w:r></w:p></w:tc></w:tr></w:tbl></w:body></w:document>''')
    source=MaterialStore(tmp_path).ingest("说明.docx",doc.getvalue())
    assert source.text=="试验目标\n\n未知" and not source.pages
    assert (tmp_path/source.asset_id/"original").read_bytes()==doc.getvalue()
    with pytest.raises(ValueError,match="DOCX"):
        MaterialStore(tmp_path).ingest("bad.docx",b"not a Word file")


def test_neighbors_keep_direct_cross_layer_feedback_but_never_private_rationale():
    class Change:
        def complete(self, config, messages):
            p=json.loads(messages[-1]["content"])
            result=json.loads(respond(messages))
            if p["task"]=="world":
                for review in result["reviews"]:
                    if review["entity_id"]=="port":
                        review.update(after="调整",reason="SECRET_REASON_ONLY_WORLD",action_ids=[p["actions"][0]["id"]])
            return json.dumps(result)
    sim=Simulation(demo_scenario(),demo_config().model_copy(update={"roster_policy":"neighbors","windows":2}),Change())
    assert sim.run()["status"]=="completed"
    views=[e["data"]["visible"] for e in sim.journal.snapshot({"observation"}) if e["window"]==2]
    engineer=next(v for v in views if v["self"]["id"]=="engineer")
    assert {"city","company","community"} <= {r["id"] for r in engineer["roster"]}
    for view in views:
        feed=view["world_feedback"]
        assert feed["window"]==1 and "SECRET_REASON_ONLY_WORLD" not in json.dumps(feed)
        assert any(c["entity_id"]=="port" for c in feed["visible_changes"]) is (view["self"]["id"]=="port")


def test_preflight_rejects_impossible_first_round_without_creating_run(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        payload=client.get("/api/demo").json()
        payload["config"]["models"]["configs"]["offline"]["context_limit_chars"]=100
        check=client.post("/api/validate",json=payload).json()["preflight"]
        assert check["blocking_context"] and check["by_model"]["offline"]==33
        assert client.post("/api/runs",json=payload).status_code==400
        assert not client.get("/api/runs").json()["runs"]


def test_stop_interrupts_infinite_backoff_preserving_last_boundary(tmp_path):
    entered=threading.Event()
    def handle(request):
        entered.set()
        return httpx.Response(503,json={"error":"temporary"})
    data=demo_config().model_dump(mode="json")
    data["models"]["configs"]["offline"]={"provider":"custom","model":"offline-mock","base_url":"http://localhost/v1",
                                          "retry_delay_seconds":30,"max_retries":None}
    sim=Simulation(demo_scenario(),RunConfig.model_validate(data),ModelGateway(transport=httpx.MockTransport(handle)),tmp_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(sim.run)
        assert entered.wait(4)
        sim.request_stop()
        snap=future.result(timeout=4)
    assert snap["status"]=="stopped" and snap["window"]==0
    assert sim.journal.snapshot({"run_aborted"})
    assert Simulation.restore(tmp_path).status=="stopped"
