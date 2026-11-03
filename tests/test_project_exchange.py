import io
from zipfile import ZipFile

import pytest
from fastapi.testclient import TestClient

from chuantuo.demo import demo_scenario, demo_config
from chuantuo.engine import Simulation
from chuantuo.experiments import branch_payload, compare
from chuantuo.schema import Scenario
from chuantuo.web import create_app


def exported(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        payload = client.get("/api/demo").json()
        asset = client.post("/api/materials",files={"file":("材料.txt","可移植的原文。".encode(),"text/plain")}).json()["source"]
        payload["scenario"]["sources"] = [asset]
        saved = client.post("/api/projects",json=payload).json()
        response = client.get(f"/api/projects/{saved['id']}/package?version=1")
        assert response.status_code == 200
        return response.content,saved,asset


def test_project_archive_round_trip_and_content_address_collision(tmp_path):
    package, original, asset = exported(tmp_path / "a")
    with TestClient(create_app(tmp_path / "b")) as client:
        def upload():
            return client.post("/api/project-import",files={"file":("archive.zip",package,"application/zip")})
        restored=upload().json()
        assert restored["project"]["payload"] == original["payload"]
        assert restored["project"]["id"] != original["id"] and restored["installed_assets"] == 1
        assert client.get(f"/api/materials/{asset['asset_id']}/original").content == "可移植的原文。".encode()
        assert upload().json()["reused_assets"] == 1
        local = tmp_path/"b"/"_materials"/asset["asset_id"]/"original"
        local.write_bytes(b"local value")
        assert upload().status_code == 400
        assert local.read_bytes() == b"local value"
        assert len(client.get("/api/projects").json()) == 2


@pytest.mark.parametrize("bad",["checksum","escape","duplicate","absent"])
def test_bad_package_installs_no_files_or_project(tmp_path,bad):
    package,_,asset=exported(tmp_path/"a")
    out=io.BytesIO()
    with ZipFile(io.BytesIO(package)) as source, ZipFile(out,"w") as dest:
        for entry in source.infolist():
            data=source.read(entry)
            if bad=="checksum" and entry.filename.endswith("/original"):data+=b"bad"
            if bad=="absent" and entry.filename.endswith("source.json"):continue
            dest.writestr(entry.filename,data)
        if bad=="escape":dest.writestr("../bad","bad")
        if bad=="duplicate":
            with pytest.warns(UserWarning):
                dest.writestr("project.json",b"{}")
    with TestClient(create_app(tmp_path/"b")) as client:
        result=client.post("/api/project-import",files={"file":("bad.zip",out.getvalue())})
        assert result.status_code==400 and not client.get("/api/projects").json()
        assert not (tmp_path/"b"/"_materials"/asset["asset_id"]).exists()
        assert not (tmp_path/"bad").exists()


def test_branch_references_frozen_state_and_comparison_distinguishes_units_and_unknowns(tmp_path):
    sim = Simulation(demo_scenario(),demo_config(),directory=tmp_path/"parent")
    sim.run()
    original=(tmp_path/"parent"/"events.jsonl").read_bytes()
    payload=branch_payload(sim,1,"新场景")
    derived=Scenario.model_validate(payload["scenario"])
    assert derived.initial_state()["city"]["readiness"]==25
    ref=derived.sources[-1].simulation_ref
    assert ref.run_id==sim.run_id and ref.window==1 and sim.journal.event(ref.event_id)["kind"]=="window_committed"
    assert (tmp_path/"parent"/"events.jsonl").read_bytes()==original
    data=derived.model_dump(mode="json")
    data["entities"][1]["fields"][0]["value"]=None
    data["entities"][2]["fields"][0]["unit"]="不同单位"
    data["entities"][3]["fields"].clear()
    new=Simulation(Scenario.model_validate(data),demo_config())
    assert all(not actor.get_state()["context_components"]["memory"]["items"] for actor in new.agents.values())
    rows={(r["actor_id"],r["field_id"]):r for r in compare(sim,new,2,0)["rows"]}
    assert rows["city","readiness"]["delta"]==-5
    assert rows["port","status"]["status"]=="unknown"
    assert rows["engineer","capacity"]["status"]=="incompatible"
    assert rows["community","access"]["status"]=="undefined"
    with pytest.raises(ValueError):
        branch_payload(sim,4,"不存在")
