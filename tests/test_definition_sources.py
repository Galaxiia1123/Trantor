import json

from fastapi.testclient import TestClient

from chuantuo.demo import demo_scenario
from chuantuo.web import create_app


def test_parse_keeps_multiple_originals_and_separate_instructions(tmp_path):
    sources = [
        {"id": "input", "name": "原始文件.md", "text": "  原文\n保留空格。\n"},
        {"id": "memo", "name": "补充材料.txt", "text": "内部材料，仅在授权后可见。"},
    ]

    class Parser:
        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            assert [{key: item[key] for key in ("id", "name", "text")} for item in payload["sources"][:2]] == sources
            assert payload["sources"][-1]["id"] == "input_request"
            scene = demo_scenario().model_dump(mode="json")
            scene["entities"][0]["provenance"] = {
                "kind": "material", "source_ids": ["input", "memo"], "explanation": "两份材料共同说明身份。"
            }
            # The parser cannot replace an original with its own paraphrase.
            scene["sources"] = [{"id": "memo", "name": "改写", "text": "不应保存这段改写"}]
            return json.dumps(scene)

    app = create_app(tmp_path, gateway=Parser())
    response = TestClient(app).post("/api/parse", json={
        "text": "识别国家内部的机构", "sources": sources,
        "model": {"provider": "demo", "model": "local-test"},
    })
    assert response.status_code == 200, response.text
    scene = response.json()["scenario"]
    assert [{key: item[key] for key in ("id", "name", "text")} for item in scene["sources"][:2]] == sources
    assert scene["sources"][-1]["text"] == "识别国家内部的机构"
    assert scene["entities"][0]["provenance"]["source_ids"] == ["input", "memo"]
    assert not app.state.runs
