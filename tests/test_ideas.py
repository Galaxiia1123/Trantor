import pytest

from chuantuo.briefing import runtime_scene, separate_brief
from chuantuo.experiments import ExperimentRequest, apply_intervention
from chuantuo.ideas import useful_ideas
from chuantuo.perception import Perception, decision_view
from chuantuo.schema import RunConfig11, Scenario11, Source
from chuantuo.source_windows import apply_meaning_tables
from chuantuo.asking import PlanQuestion, plan_question
from chuantuo.engine import Simulation


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


def test_opening_knowledge_in_plain_prose_is_scoped_to_new_preparation():
    source=Source(id="doc",name="公告计划",text=(
        "这是虚构背景。双方开局都知道第2窗计划发布公告，内部备用口令只由甲掌握。\n"
        "全员第3窗才获知延期。全员开局不知道备用口令。\n"
        "> 全员开局知道不应采用的引文。\n"
        "## 示例\n全员开局知道不应采用的示例。\n"
        "## 正式材料\n全员开局已知道可以主动询问。"
    ))
    scene={"title":"公告","background":"全局设定","domains":["公告"],
           "entities":[{"id":"a","name":"甲","identity":"甲"},{"id":"b","name":"乙","identity":"乙"}],
           "sources":[source.model_dump()],"scheduled_events":[{"id":"notice","window":2,"text":"实际发布公告"}]}
    historical=Scenario11.model_validate(scene)
    assert runtime_scene(historical).public_brief.text==""
    prepared=historical.model_dump(mode="json")
    separate_brief(prepared,[source],include_opening_statements=True)
    opening=prepared["public_brief"]
    assert opening["text"]=="双方开局都知道第2窗计划发布公告\n全员开局已知道可以主动询问"
    assert all(q["quote"] in source.text for q in opening["provenance"]["quotes"])
    updated=Scenario11.model_validate(prepared)
    cancelled,_=apply_intervention(updated,ExperimentRequest(kind="event_cancel",window=0,through=2,
                                   event_move={"event_id":"notice"}),"cancel")
    assert not cancelled.scheduled_events and cancelled.public_brief==updated.public_brief
    three=historical.model_dump(mode="json")
    three["entities"].append({"id":"c","name":"丙","identity":"丙"})
    separate_brief(three,[source],include_opening_statements=True)
    assert "第2窗" not in three["public_brief"]["text"]


def test_explicit_levels_keep_the_authors_timing_and_check_every_value():
    text="我想在第 1 窗末把席位分别设为 6、12、18 个，比较第 2 窗末的安排。"
    scene=Scenario11.model_validate({"title":"夜课","background":"讨论夜课。","sources":[{"id":"doc","name":"设定","text":text}],
        "domains":["排课"],"entities":[{"id":"desk","name":"书房","identity":"排课","fields":[
        {"id":"seats","label":"席位","kind":"number","domain":"排课","unit":"个","value":6,"constraints":{"maximum":24}},
        {"id":"plan","label":"安排","kind":"string","domain":"排课","value":"待定"}]}]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":2})
    proposal={"question":"试不同席位","experiment":{"kind":"field","window":1,"through":2,"assignment":{
        "entity_id":"desk","field_id":"seats","value":12}},"additional_values":[6,18]}
    ideas=useful_ideas(scene,cfg,[proposal])
    assert len(ideas)==1 and ideas[0]["experiment"]["window"]==0 and ideas[0]["additional_values"]==[12,18]
    assert ideas[0]["quote"]["quote"]==text and ideas[0]["watch"]["field_id"]=="plan"
    assert not useful_ideas(scene.model_copy(update={"sources":[]}),cfg,[{**proposal,"additional_values":[6,250]}])


def test_value_meanings_keep_the_source_quote_and_follow_field_visibility():
    line="| 书房 | 进度 | 通过 | 负责人实际登记即可。 |"
    columns="| 主体 | 字段 | 候选值 | 含义 |\n| --- | --- | --- | --- |\n"
    scene={"title":"登记","background":"各方独立。","domains":["排课"],"missing_information":[],
           "sources":[{"id":"draft","name":"设定","text":columns+line}],
           "entities":[{"id":"a","name":"书房","identity":"登记","fields":[{"id":"state","label":"进度","kind":"string","domain":"排课",
                        "value":"待定","constraints":{"choices":["待定","通过"]}}]}, {"id":"b","name":"来访者","identity":"咨询","fields":[]}]}
    record={};apply_meaning_tables(scene,record);cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"}})
    s=Scenario11.model_validate(scene);p=Perception(s,s.depths(),cfg)
    assert s.entities[0].fields[0].constraints.meanings[0].provenance.quotes[0].quote==line
    assert "负责人实际登记" in str(decision_view(p.observe("a",s.initial_state(),[],1,1,None)))
    assert "负责人实际登记" not in str(decision_view(p.observe("b",s.initial_state(),[],1,1,None)))
    scene["sources"].append({"id":"other","name":"新文稿","text":columns+line.replace("负责人实际登记即可。","须等首场开课。")})
    conflict={};apply_meaning_tables(scene,conflict);assert len(conflict["blocking_questions"])==1


def test_observer_question_checks_units_limits_and_named_readers_without_touching_the_run(tmp_path):
    s=Scenario11.model_validate({"title":"排课","background":"两方协商。","domains":["排课"],"entities":[
        {"id":"room","name":"社区书房","identity":"排课","fields":[{"id":"seats","label":"席位","kind":"number","value":8,
        "unit":"个","domain":"排课","constraints":{"maximum":20}}]},{"id":"teacher","name":"木刻教师","identity":"授课"}]})
    cfg=RunConfig11.model_validate({"model":{"provider":"demo","model":"fixture"},"windows":2,"rounds_per_window":1})
    sim=Simulation(s,cfg,directory=tmp_path);assert sim.run()["status"]=="completed";before=sim.snapshot()
    def ask(text):return plan_question(sim,PlanQuestion(question=text,default_split=0))
    ok=ask("如果把席位改为16个，只通知社区书房。")
    assert not ok["calls"] and ok["ideas"][0]["experiment"]["readers"]==["room"]
    assert not ask("如果把席位改为30个。")["ideas"]
    assert "单位" in ask("如果把席位改为16元。")["clarification"]
    assert "主体名称" in ask("如果把席位改为16个，只通知那个人。")["clarification"]
    assert sim.snapshot()["state"]==before["state"] and sim.snapshot()["model_calls"]==before["model_calls"]
