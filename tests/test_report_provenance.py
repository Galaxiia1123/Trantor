import json

import pytest

from chuantuo.demo import demo_config, demo_scenario
from chuantuo.engine import Simulation
from chuantuo.services import generate_report
from chuantuo.services import plan_report
from chuantuo.repository import Repository


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


def test_report_uses_confirmed_questions_and_frozen_observation_results(tmp_path):
    sim=Simulation(demo_scenario(),demo_config().model_copy(update={"windows":1}),directory=tmp_path)
    sim.run()
    repository=Repository(tmp_path)
    question={"actor_id":"city","field_id":"readiness","operator":"at_least","target":50000,
              "criterion":"at_end","deadline":3,"title":"期限前是否就绪"}
    repository.field_watches(tmp_path,"b"*32,question)
    plan=plan_report(sim)
    repository.field_watches(tmp_path,"b"*32,{**question,"title":"之后修改的问题"})

    class Reporter:
        calls=0
        def complete(self,config,messages):
            self.calls+=1
            payload=json.loads(messages[-1]["content"])
            row=payload["execution"]["observation_questions"][0]
            assert row["question"]["title"]=="期限前是否就绪"
            assert row["status"]=="ended_early" and row["through"]==1
            assert row["record"]==sim.journal.settlement(1)["id"]
            assert "*** End Patch" not in messages[0]["content"]
            return json.dumps({"title":"问题回顾","sections":[{"title":"摘要","text":"记录止于第一窗。",
                               "evidence_ids":[row["record"]]}]},ensure_ascii=False)

    gateway=Reporter()
    result=generate_report(sim,gateway,questions=plan["questions"])
    assert gateway.calls==1
    assert "期限前是否就绪" in result["markdown"] and "之后修改的问题" not in result["markdown"]
    assert "记录未覆盖整个期限" in result["markdown"]
    assert result["coverage"]["observation_questions"][0]["status"]=="ended_early"
    assert f'id="event-{sim.journal.settlement(1)["id"]}"' in result["markdown"]
    with pytest.raises(ValueError,match="已经定义的字段"):
        generate_report(sim,gateway,questions=[{**question,"field_id":"undefined"}])
    assert gateway.calls==1
