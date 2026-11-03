import pytest

from chuantuo.briefing import runtime_scene
from chuantuo.experiments import ExperimentRequest, apply_intervention
from chuantuo.ideas import useful_ideas
from chuantuo.perception import Perception, decision_view
from chuantuo.schema import RunConfig11, Scenario11, Source


def test_source_question_moves_a_future_event_without_disclosing_the_private_calendar():
    source=Source(id="doc",name="计划",text="## 公开背景\n两个办事处分别排班。\n## 内部安排\n如果把供电切换从第 2 窗推迟到第 3 窗，第 2 窗末时长是多少？")
    event={"id":"power","window":2,"text":"楼宇设备自动切换供电计划。","visibility":{"readers":["team"]},
           "assignments":[{"entity_id":"team","field_id":"minutes","value":30}]}
    scene=Scenario11.model_validate({"title":"排班","background":"管理处的时长为90分钟，第二窗切换供电。","sources":[source.model_dump()],
        "domains":["排班"],"entities":[{"id":"team","name":"管理处","identity":"安排排班","fields":[
        {"id":"minutes","label":"时长","domain":"排班","kind":"number","value":90,"unit":"分钟"}]},
        {"id":"guest","name":"访客","identity":"申请时段"}],"scheduled_events":[event]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":3})
    active=runtime_scene(scene);p=Perception(active,active.depths(),cfg)
    other=decision_view(p.observe("guest",active.initial_state(),[],1,1,None))
    assert other["background"]=="两个办事处分别排班。" and "90分钟" not in str(other)
    original=scene.model_dump(mode="json");original["public_brief"]={"text":"管理处的时长为90分钟。"}
    with pytest.raises(ValueError,match="私有字段"):
        Scenario11.model_validate(original)
    idea=useful_ideas(active,cfg)[0]
    assert idea["origin"]=="material" and idea["quote"]["quote"] in source.text and idea["watch"]["field_id"]=="minutes"
    spec=ExperimentRequest.model_validate(idea["experiment"])
    assert (spec.window,spec.through)==(1,2)
    alternate,_=apply_intervention(active,spec,"sample")
    assert alternate.scheduled_events[0].window==3 and active.scheduled_events[0].window==2
    assert alternate.scheduled_events[0].visibility==active.scheduled_events[0].visibility
    with pytest.raises(ValueError,match="尚未发生"):
        apply_intervention(active,spec.model_copy(update={"window":2,"through":3}),"sample")
