import time

from fastapi.testclient import TestClient

from chuantuo.web import create_app


def test_http_end_to_end_demo_run_report_export_and_validation(tmp_path):
    client = TestClient(create_app(tmp_path))
    payload = client.get("/api/demo").json()
    assert client.post("/api/validate", json=payload).json()["depths"]["engineer"] == 2
    payload["config"]["windows"] = 1
    response = client.post("/api/runs", json=payload)
    assert response.status_code == 200
    run_id = response.json()["run_id"]
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        snapshot = client.get(f"/api/runs/{run_id}").json()
        if snapshot["status"] not in ("ready", "running"):
            break
        time.sleep(0.01)
    assert snapshot["status"] == "completed"
    assert snapshot["state"]["city"]["readiness"] == 25
    events = client.get(f"/api/runs/{run_id}/events").json()
    assert events["next_offset"] == len(events["events"])
    assert client.get(f"/api/runs/{run_id}/events?after={events['next_offset']}").json()["events"] == []
    report = client.post(f"/api/runs/{run_id}/report")
    assert report.status_code == 200
    assert "离线流程验证" in report.json()["markdown"]
    assert client.get(f"/api/runs/{run_id}/export").status_code == 200
    assert client.post(f"/api/runs/{run_id}/resume").status_code == 409
    assert client.get("/api/runs/missing").status_code == 404


def test_browser_cross_origin_cannot_spend_credits_or_create_run(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    payload = client.get("/api/demo").json()
    response = client.post("/api/runs", json=payload, headers={"origin": "https://another-site.example"})
    assert response.status_code == 403
    assert not app.state.runs


def test_parse_and_weight_estimate_do_not_start_a_simulation(tmp_path):
    app = create_app(tmp_path)
    client = TestClient(app)
    model = {"provider": "demo", "model": "scripted-demo"}
    parsed = client.post("/api/parse", json={"text": "用户材料原文", "model": model})
    assert parsed.status_code == 200
    assert parsed.json()["requires_review"]
    scenario = parsed.json()["scenario"]
    scenario["influences"][0]["weight"] = None
    estimated = client.post("/api/estimate", json={"scenario": scenario, "model": model})
    assert estimated.status_code == 200
    assert estimated.json()["scenario"]["influences"][0]["weight"] == 0.5
    assert not app.state.runs
