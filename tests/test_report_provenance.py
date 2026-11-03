import json

import pytest

from chuantuo.demo import demo_config, demo_scenario
from chuantuo.engine import Simulation
from chuantuo.services import generate_report


@pytest.mark.parametrize("has_receipts", [True, False])
def test_report_distinguishes_source_history_from_execution_receipts(tmp_path, has_receipts):
    # Entirely local fixture: the synthetic receipt event tests provenance
    # precedence without contacting or claiming to have contacted a provider.
    sim = Simulation(demo_scenario(), demo_config().model_copy(update={"windows": 1}), directory=tmp_path)
    sim.run()
    audit = None
    if has_receipts:
        audit = sim.journal.append("runtime_verification", {
            "source": "test fixture; no network requests",
            "actual_response_models": ["fixture-decision-model"],
            "http_status_counts": {"200": 36},
        }, sim.window)

    class Reporter:
        def complete(self, config, messages):
            payload = json.loads(messages[-1]["content"])
            original = next(e for e in payload["events"] if e["kind"] == "run_created")
            assert "离线脚本演示" in original["data"]["scenario"]["background"]
            audits = [e for e in payload["events"] if e["kind"] == "runtime_verification"]
            assert len(audits) == int(has_receipts)
            assert "只描述原材料" in messages[0]["content"]
            return json.dumps({"title": "报告", "sections": [{"title": "来源", "text": "沿用虚构示例。",
                               "evidence_ids": [original["id"]]}]})

    report = generate_report(sim, Reporter())
    if audit:
        assert "调用方式核验" in report["markdown"]
        assert "36 次" in report["markdown"] and "fixture-decision-model" in report["markdown"]
        assert f'id="event-{audit["id"]}"' in report["markdown"]
    else:
        assert "调用方式核验" not in report["markdown"]
