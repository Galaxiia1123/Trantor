import json
from copy import deepcopy
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from chuantuo.engine import Simulation
from chuantuo.insights import watch_window
from chuantuo.models import ModelConfig, ModelError, configured_key
from chuantuo.perception import decision_view
from chuantuo.preparation import Seed, _dated_for, prepare
from chuantuo.profile import ModelProfile, ProfileInput
from chuantuo.public_sources import PublicSearch, read_public_page
from chuantuo.repository import Repository
from chuantuo.schema import Scenario11, load_scenario
from chuantuo.settlement import verify_commits
from chuantuo.web import create_app
from scripts.v11_fixture import CONDITION, FixtureGateway, build, definition, sample_source


@pytest.fixture(scope="module")
def native_run(tmp_path_factory):
    return build(tmp_path_factory.mktemp("v11")/"case")[0]


def scene_dict():
    source = sample_source()
    scene = definition({"sources":[source.model_dump()]})["scenario"]
    scene["sources"] = [source.model_dump()]
    return scene


def test_counterfactual_reopens_with_original_private_memory_and_only_new_usage(native_run,tmp_path):
    from chuantuo.experiments import ExperimentRequest, experiment_comparison, fork_experiment
    marker="试验签件在第八窗送达市议会"
    spec=ExperimentRequest(window=7,through=9,condition=marker,audience="readers",readers=["council"])
    path=tmp_path/"route"
    variant=fork_experiment(native_run,spec,"e"*12,None,path)
    assert variant.window==7 and variant.snapshot()["model_calls"]==0 and not variant.journal.usage()
    assert variant.run()["status"]=="completed"
    restored=Simulation.restore(path)
    assert restored.snapshot()["model_calls"]==22 and restored.experiment["parent_id"]==native_run.run_id
    for item in restored.journal.snapshot({"model_input"}):
        obs=json.loads(item["data"]["messages"][-1]["content"])
        assert (marker in str(obs))==(item["window"]>7 and (obs["task"]=="world" or obs.get("actor_id")=="council"))
    context=restored.journal.decision_context("engineer",8,1,restored._discarded)
    prior=native_run.journal.decision_context("engineer",8,1,native_run._discarded)
    assert context["memory"]==prior["memory"] and context["observation"]==prior["observation"]
    result=experiment_comparison(native_run,restored,8)
    assert result["first_state_difference"] is None and result["baseline"]["actions"] and result["variant"]["actions"]


def test_containment_simplification_keeps_ownership_and_directed_channels():
    data = scene_dict()
    base = load_scenario(data)
    assert base.depths()["engineer"] == 2 and not base.subsystems
    assert base.entities[1].parent_id == "council"
    assert any(e.source == "engineer" and e.target == "council" for e in base.influences)
    data["entities"][4]["subsystem_id"] = "public_service"
    assert not Scenario11.model_validate(data).subsystems
    data["subsystems"][0]["interaction"] = "港务署与居民代表共同安排夜间航道听证"
    assert len(Scenario11.model_validate(data).subsystems) == 1
    data["entities"][0]["parent_id"] = "engineer"
    with pytest.raises(ValidationError,match="循环"): Scenario11.model_validate(data)
    data = scene_dict()
    data["influences"][0]["weight"] = .8
    with pytest.raises(ValidationError): Scenario11.model_validate(data)


def test_private_long_memory_survives_eviction_and_replay(native_run):
    sim = native_run
    inputs = [e for e in sim.journal.snapshot({"model_input"}) if e["data"]["purpose"] == "agent"]
    assert len(inputs) == 90 and sim.config.windows == 9 and sim.config.rounds_per_window == 2
    for e in inputs:
        d = json.loads(e["data"]["messages"][-1]["content"])
        assert "visible_state" not in d["observation"] and '"weight"' not in json.dumps(d)
        assert len(d["memory"]["recent"]) <= sim.config.recent_records
        if d["actor_id"] not in {"engineer","carrier"}:
            assert "潮汐-17" not in str(d) and "担保上限为2000" not in str(d)
        if d["actor_id"] == "engineer" and d["window"] == 7 and d["round"] == 2:
            matter = d["memory"]["open_matters"]["guarantee"]
            assert matter["text"] == CONDITION and matter["window"] == 1
            assert all(r["window"] > 1 for r in d["memory"]["recent"])
            assert sim.journal.event(matter["record"])["data"]["actor_id"] == "engineer"
            assert "资金缺口为2000万元" in d["observation"]["state"]["council"]
    deliveries = sim.journal.snapshot({"message_delivered"})
    assert any(d["data"]["from"]=="engineer" and d["data"]["to"]=="council" and d["window"]==7 for d in deliveries)
    assert all(CONDITION not in d["data"]["text"] for d in deliveries if d["data"]["to"] not in {"engineer","carrier"})
    assert sim.journal.usage()["主体"]["calls"] == 90 and sim.journal.usage()["世界"]["calls"] == 9
    restored = Simulation.restore(sim.journal.directory,FixtureGateway())
    assert restored.state == sim.state
    for actor,book in restored.notebooks.items():
        before = sim.notebooks[actor]
        assert (book.records,book.open,list(book.older)) == (before.records,before.open,list(before.older))
    assert "guarantee" not in restored.notebooks["engineer"].open
    inspected=restored.journal.decision_context("engineer",7,2,set())
    assert inspected["memory"]["open_matters"]["guarantee"]["text"]==CONDITION
    assert inspected["rounds"]==[1,2] and restored.journal.event(inspected["action_id"])["data"]["model_output_id"]==inspected["output_id"]


def test_current_state_prose_and_details_share_a_commit(native_run):
    from chuantuo.observatory import FieldWatch, observe_field, ongoing_matters
    q=FieldWatch(actor_id="council",field_id="agenda",operator="equals",target="已列入复议",deadline=8)
    before_result=observe_field(native_run,q,7)
    after_result=observe_field(native_run,q,9)
    assert before_result["status"]=="pending" and before_result["first_hit"] is None
    assert after_result["through"]==8 and after_result["first_hit"]["window"]==8 and after_result["segments"][-1]["actions"]
    throughout=observe_field(native_run,q.model_copy(update={"criterion":"always"}),8)
    final=q.model_copy(update={"criterion":"at_end"})
    assert throughout["status"]=="violated" and throughout["first_miss"]["window"]==0
    assert observe_field(native_run,final,7)["status"]=="pending_end" and observe_field(native_run,final,8)["status"]=="met_at_end"
    matters=ongoing_matters(native_run,7,"engineer","open")
    assert all(note["actor_id"]=="engineer" for note in matters["items"])
    assert any("2000" in note["text"] for note in matters["items"])
    before, after = (watch_window(native_run, i) for i in (7,8))
    council = next(a for a in after["actors"] if a["id"] == "council")
    assert "已列入复议" in council["profile"] and "资金缺口为2000万元" in council["profile"]
    assert "议案已列入复议" not in before["actors"][0]["profile"]
    assert before["actors"][0]["fields"]["agenda"]["value"]=="等待复核和书面担保"
    assert native_run.journal.event(after["state_record"])["data"]["state"]["council"]["gap"] == council["fields"]["gap"]["value"] == 2000
    observation = native_run.journal.private_history("council",9,last_observation=True)[0]
    assert decision_view(observation["data"]["visible"])["state"]["council"] == council["profile"]
    assert set(native_run.config.model_dump()).isdisjoint({"models","layers"})
    assert all(e["data"]["config_id"] == "shared" for e in native_run.journal.snapshot({"model_input","report_input"}))


def test_research_lookup_dates_and_original_not_search_snippet():
    rss = "<rss><channel><item><title>海港办法</title><link>https://example.org/regulation</link><description>未经核实的摘要</description></item></channel></rss>"
    transport = httpx.MockTransport(lambda req: httpx.Response(200,text=rss))
    src, errors = PublicSearch(transport,lambda url:(url,"签署文书后方可申请复议。","2020-05-01")).search("海港审议办法","web")
    assert len(src) == 1 and not errors and "未经核实" not in src[0].text
    assert _dated_for(src[0],date(2021,1,1)) and not _dated_for(src[0],date(2020,4,1))
    for addr in ["https://127.0.0.1/private","http://example.org","https://user:pass@example.org","https://[::1]","https://224.0.0.1","https://[::ffff:127.0.0.1]"]:
        with pytest.raises(ValueError): read_public_page(addr)


class ResearchCase:
    def __init__(self, use=True, public=True):
        self.tasks, self.use, self.public = [], use, public

    def complete(self, model, messages):
        payload = json.loads(messages[-1]["content"])
        self.tasks.append((model.model,payload["task"]))
        s = payload["sources"][0]
        p = {"kind":"material","source_ids":[s["id"]],"quotes":[{"source_id":s["id"],"quote":"文献复核"}]}
        data = {"summary":"编辑部讨论是否复核文献。","plan":{},"plan_reason":"给成员留出一次回应机会",
                "scenario":{"title":"文献复核","background":"编辑部准备复核。","entities":[{"id":"editor","name":"编辑","identity":"评估文献","provenance":p}],"domains":[],"rules":[]},
                "checklist":dict.fromkeys(["setting","actors_and_preferences","domains_and_state","containment","directed_relations","rules_and_visibility","time_and_objective"], "irrelevant"),
                "gaps":[{"id":"publication","kind":"public" if self.public else "intent","question":"复核依据是什么？","query":"public governance paper" if self.public else "",
                         "collection":"research","critical":True,"as_of":"2023-12-01"}]}
        if payload["task"] == "complete_scene" and self.use:
            candidate = payload["public_candidates"][0]; quote = {"source_id":candidate["id"],"quote":"Public decision review"}
            data["scenario"]["rules"] = [{"text":"采用该篇文献作为讨论依据","provenance":{"kind":"material","source_ids":[quote["source_id"]],"quotes":[quote]}}]
            data["findings"] = [{"gap_id":"publication","quote":quote,"relevance":"标题对应拟复核文献","time_and_authority":"登记日期早于情景，登记资料只证明出版信息"}]
        return json.dumps(data,ensure_ascii=False)


def catalog_response(req):
    return httpx.Response(200,json={"message":{"items":[
        {"DOI":"10.1000/test","title":["Public decision review"],"published":{"date-parts":[[2023,3,1]]}},
        {"DOI":"10.1000/new","title":["Later edition"],"published":{"date-parts":[[2024]]}}]}})


@pytest.mark.parametrize("enabled,found,public,questions,steps",[
    (True, True, True,0,2), (True,False,True,1,2), (False,True,True,1,1), (True,False,False,1,1)])
def test_seed_search_checklist_and_necessary_questions(enabled,found,public,questions,steps):
    model = ModelConfig(provider="custom",model="shared-test",base_url="http://localhost")
    driver = ResearchCase(found, public); search = PublicSearch(httpx.MockTransport(catalog_response))
    result = prepare(Seed(background="文献复核。运行 12 个窗口。",allow_search=enabled,overrides={"windows":4}),
                     model,driver,None,lambda *_:None,search)
    record = result["preparation_records"][0]
    assert result["config"]["windows"] == 4 and len(driver.tasks) == steps and len(result["questions"]) == questions
    assert {k for k,_ in driver.tasks} == {"shared-test"}
    if not questions:
        sources = [s for s in result["scenario"]["sources"] if s["public_reference"]]
        assert len(sources) == 1 and sources[0]["public_reference"]["url"] == "https://doi.org/10.1000/test"
        assert record["findings"][0]["quote"] == result["scenario"]["rules"][0]["provenance"]["quotes"][0]
    else:
        assert not any(s["public_reference"] for s in result["scenario"]["sources"])


def test_profile_key_is_bound_to_endpoint_and_never_exported(tmp_path):
    profile = ModelProfile(tmp_path); secret = "local-fixture-key-private-11"
    model = ModelConfig(provider="custom",base_url="http://127.0.0.1:9311",model="test")
    response = profile.save(ProfileInput(model=model,key=secret))
    assert response["ready"] and secret not in str(response) and secret not in profile.path.read_text(encoding="utf-8")
    assert configured_key(profile.model) == secret
    assert not configured_key(profile.model.model_copy(update={"base_url":"http://127.0.0.1:9312"}))
    import os
    if os.name == "nt":
        from chuantuo import models
        with models._profiles_lock:
            models._profile_keys.pop(profile.model.api_key_env)
        assert configured_key(ModelProfile(tmp_path).model) == secret


def test_relation_change_needs_reason_and_keeps_revision(tmp_path,native_run):
    repo = Repository(tmp_path); bundle = {"scenario":native_run.scenario.model_dump(mode="json"),"config":native_run.config.model_dump(mode="json"),"assistant":{}}
    first = repo.save_project(bundle); edited = deepcopy(bundle)
    edited["scenario"]["influences"][0]["description"] = "只能递交通过签字的书面意见。"
    with pytest.raises(ValueError,match="依据"): repo.save_project(edited,first["id"],1)
    edited["assistant"]["revision_reason"] = "听证双方新增签字条件"
    next_version = repo.save_project(edited,first["id"],1)
    assert next_version["version"] == 2 and next_version["relation_revision"]["changes"][0]["before"] == bundle["scenario"]["influences"][0]


def test_root_and_readonly_sample_without_profile(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        assert "准备一场推演" in client.get("/").text and "assistant-goal" in client.get("/archive").text
        assert not client.get("/api/model").json()["ready"]
        replay = client.get("/api/case/replay").json()
        assert replay["kind"] == "scripted_replay" and len(replay["windows"]) == 10
        assert client.get("/api/runs").json()["runs"] == []
        response = client.post("/api/jobs/"+"a"*32+"/prepare",json={"background":"一个虚构场景"})
        assert response.status_code == 400


def test_revision_preserves_user_owned_directed_limit():
    model = ModelConfig(provider="custom",model="fixture",base_url="http://localhost")
    current = scene_dict()
    relation = current["influences"][0]
    relation.update(description="只能在次日十二点前提交一份签字意见。",provenance={"kind":"user","explanation":"用户新增时间权限"})
    original = Scenario11.model_validate(current)
    result = prepare(Seed(current_scenario=original,background="用原有材料重新整理。"),model,FixtureGateway(),None,lambda *_:None)
    kept = next(e for e in result["scenario"]["influences"] if e["source"]=="engineer")
    assert kept["description"] == relation["description"] and kept["provenance"]["kind"] == "user"
    assert len(result["scenario"]["sources"]) == 2


def test_private_marker_in_world_rule_never_reaches_unaddressed_actor():
    from chuantuo.perception import Perception
    from chuantuo.schema import RunConfig11
    sample = scene_dict()
    sample["entities"][3]["materials"][0]["protected_terms"] = ["潮汐-17"]
    sample["rules"].append({"text":"代号潮汐-17只能保存在私下往来。","visibility":{"readers":["carrier"]}})
    config = RunConfig11(model=ModelConfig(provider="custom",model="test",base_url="http://localhost"))
    scene = Scenario11.model_validate(sample)
    perception = Perception(scene,scene.depths(),config)
    for actor in ("resident","carrier"):
        view = perception.observe(actor,scene.initial_state(),[],1,1,None)
        assert ("潮汐-17" in json.dumps(view,ensure_ascii=False)) == (actor == "carrier")
    for public in ({"visibility":{"public":True}}, {"visibility":{"readers":["resident"]}}):
        sample["rules"][-1].update(public)
        with pytest.raises(ValidationError,match="读者权限"): Scenario11.model_validate(sample)
    sample["rules"][-1].pop("visibility")
    scene = Scenario11.model_validate(sample)
    assert not scene.rules[-1].visibility.public and not scene.rules[-1].visibility.readers
    sample["background"] += "潮汐-17"
    with pytest.raises(ValidationError,match="公共背景"): Scenario11.model_validate(sample)


def test_explicit_source_fields_relation_direction_and_negative_example():
    from chuantuo.source_contract import reconcile
    text = "记录字段“回函状态”初始为待回复。不得创建字段“声望”。策展组可向排练组交付灯光清单。并非排练组可向策展组承诺演出日期。"
    p = {"kind":"material","source_ids":["brief"],"quotes":[{"source_id":"brief","quote":"回函状态"}]}
    scene = Scenario11.model_validate({"title":"舞台排演","background":"安排演出。","sources":[{"id":"brief","name":"说明","text":text}],
        "domains":["协作"],"entities":[{"id":"curators","name":"望城策展组","identity":"筹备","fields":[
            {"id":i,"label":label,"domain":"协作","kind":"string","value":"待回复","provenance":p}
            for i,label in (("reply","回函状态"),("wish","参演意向"),("score","声望"))]},
            {"id":"performers","name":"青堤排练组","identity":"排演"}]})
    reviewed = reconcile({"scenario":scene.model_dump(mode="json"),"preparation_records":[{"kind":"scene_parse"}]})
    assert reconcile(reviewed) == reviewed
    s = reviewed["scenario"]; r = reviewed["preparation_records"][0]["source_reconciliation"]
    assert [f["id"] for f in s["entities"][0]["fields"]] == ["reply"]
    assert [x["field"]["id"] for x in r["suggested_fields"]] == ["wish"] and r["excluded_fields"] == ["声望"]
    assert [(e["source"], e["target"]) for e in s["influences"]] == [("curators","performers")]
    assert s["influences"][0]["provenance"]["quotes"][0]["quote"] in text


def test_known_public_amount_and_restricted_document_have_different_scopes():
    from chuantuo.schema import Decision, validate_outbox
    scene = scene_dict()
    m = scene["entities"][3]["materials"][0]
    m.update(protected_terms=["潮汐-17","2000万元"], sharing={"excluded_recipients":["resident"], "provenance":{"kind":"user"}})
    scene["background"] += "公开的资金缺口为2000万元。"
    s = Scenario11.model_validate(scene)
    assert set(s.delivery_limits()) == {"潮汐-17"}
    def mail(to,text):
        return Decision(text="按权限答复",messages=[{"recipients":to,"text":text}])
    validate_outbox(mail(["engineer"],CONDITION),s.delivery_limits())
    validate_outbox(mail(["resident"],"公开的资金缺口为2000万元。"),s.delivery_limits())
    with pytest.raises(ValueError,match="不能投递给 resident"):
        validate_outbox(mail(["council","resident"],CONDITION),s.delivery_limits())


def test_declared_grant_requires_evidence_of_initial_knowledge():
    from chuantuo.source_contract import reconcile
    scene = scene_dict()
    source = scene["sources"][0]
    m = scene["entities"][3]["materials"][0]; m["provenance"] = {"kind":"material","source_ids":[source["id"]],
        "quotes":[{"source_id":source["id"],"quote":"否则到期失效"}]}
    m["visibility"] = {"public":False,"readers":["engineer","resident"],"reader_evidence":[
        {"reader_id":"engineer","quote":{"source_id":source["id"],"quote":"海岬航运可向许澄发送附条件的担保意向"}}]}
    result = reconcile({"scenario":Scenario11.model_validate(scene).model_dump(mode="json"),"preparation_records":[{}]})
    assert not result["scenario"]["entities"][3]["materials"][0]["visibility"]["readers"]
    assert set(result["preparation_records"][0]["visibility_review"]["pending_grants"][-1]["readers"]) == {"engineer","resident"}
    statement = "许澄已收到担保意向的副本。"; source["text"] += statement
    m["visibility"]["reader_evidence"][0]["quote"]["quote"] = statement
    r = reconcile({"scenario":Scenario11.model_validate(scene).model_dump(mode="json"),"preparation_records":[{}]})
    assert r["scenario"]["entities"][3]["materials"][0]["visibility"]["readers"] == ["engineer"]


def test_forbidden_outgoing_message_is_corrected_before_any_delivery(tmp_path,native_run):
    class AttemptDisclosure(FixtureGateway):
        def complete(self,model,messages):
            raw = super().complete(model,messages)
            data = json.loads(messages[-1]["content"])
            if data["task"]=="agent" and data["actor_id"]=="carrier" and data["window"]==data["round"]==1 and "output_correction" not in data:
                action = json.loads(raw)
                action["messages"][0]["recipients"].append("resident")
                return json.dumps(action,ensure_ascii=False)
            return raw
    data = native_run.scenario.model_dump(mode="json")
    mat = next(a for a in data["entities"] if a["id"]=="carrier")["materials"][0]
    mat.update(protected_terms=["潮汐-17"],sharing={"excluded_recipients":["resident"]})
    config = native_run.config.model_copy(update={"windows":2})
    sim = Simulation(Scenario11.model_validate(data),config,AttemptDisclosure(),tmp_path/"outbox")
    assert sim.run()["status"] == "completed"
    errors = sim.journal.snapshot({"model_validation_error"})
    assert [e["data"]["issues"][0]["type"] for e in errors] == ["recipient_scope"]
    assert any("resident" in e["data"]["text"] and CONDITION in e["data"]["text"] for e in sim.journal.snapshot({"model_output"}))
    sent = [e["data"] for e in sim.journal.snapshot({"message_delivered"}) if "潮汐-17" in e["data"]["text"]]
    assert [m["to"] for m in sent] == ["engineer"] and "resident" not in sent[0]["sharing"]["潮汐-17"]
    assert Simulation.restore(sim.journal.directory).state == sim.state


def test_structure_repair_never_changes_words_or_accepts_incomplete_or_ambiguous_json():
    from chuantuo.structured_output import decode_object
    raw = '{"entities":[{"name":"A ] \\\" B","rule":{"text":"remain"}}]}'
    data, edits = decode_object(raw.replace('"}}]', '"}]'))
    assert data == json.loads(raw) and len(edits)==1 and edits[0]["inserted"]=="}"
    for invalid in ('{"a":1,"a":2}', '{"actors":[{"name":"A"}', '{"text":"unfinished', '{"a":1 "b":2}'):
        with pytest.raises(ValueError): decode_object(invalid)


def test_recheck_original_corrected_draft_without_new_calls(tmp_path,monkeypatch):
    from chuantuo import preparation
    from chuantuo.preparation_trace import PreparationGateway
    seed = Seed(sources=[sample_source()])
    model = ModelConfig(provider="custom",model="capture",base_url="http://localhost")
    class Captures(FixtureGateway):
        def complete(self,model,messages):
            if "correction" not in json.loads(messages[1]["content"]):
                return "invalid"
            valid = super().complete(model,messages)
            return valid.replace("}]","]",1)
    first = PreparationGateway(Captures(),tmp_path,"first")
    with monkeypatch.context() as patch:
        patch.setattr(preparation,"decode_object",lambda raw:(json.loads(raw),[]))
        with pytest.raises(ValueError): prepare(seed,model,first,None,lambda *_:None)
    assert first.calls == 2
    class NoNetwork:
        def complete(self,*args): raise AssertionError("unexpected new provider request")
    second = PreparationGateway(NoNetwork(),tmp_path,"second","first")
    result = prepare(seed,model,second,None,lambda *_:None)
    assert second.calls == 0 and second.rechecked == 2 and not result["questions"]
    assert second.journal.usage()["准备"]["calls"] == 0
    assert result["preparation_records"][0]["format_repairs"]


def test_material_claim_can_link_exact_frozen_source_without_rebilling(tmp_path):
    from chuantuo.services import _report_inputs, generate_report, plan_report
    sim, _, _ = build(tmp_path/"report-run")
    snapshot,_,model_id,_,messages,_,_ = _report_inputs(sim)
    obs = sim.journal.snapshot({"observation"})[0]["id"]
    src = sim.scenario.sources[0].id
    raw = json.dumps({"title":"核对材料","sections":[{"title":"来源","text":"据原件设置场景","evidence_ids":[obs],"claims":[
        {"basis":"source","text":"材料描述潮汐港扩建","evidence_ids":[obs],"source_ids":[src]}]}]},ensure_ascii=False)
    call = sim.journal.append("report_input",{"config_id":model_id,"messages":messages},snapshot["window"])
    out = sim.journal.append("report_output",{"input_id":call["id"],"text":raw},snapshot["window"])
    sim.journal.append("model_validation_error",{"purpose":"report","output_id":out["id"],"issues":[{"msg":"旧校验缺少来源锚点"}]},snapshot["window"])
    class NoNetwork:
        def complete(self,*args): raise AssertionError("unexpected new provider request")
    assert plan_report(sim)["minimum_new_calls"] == 0
    result = generate_report(sim,NoNetwork())
    assert result["coverage"]["new_model_calls"] == 0 and result["coverage"]["rechecked_output_id"] == out["id"]
    assert result["report"]["sections"][0]["claims"][0]["evidence_ids"] == [obs,"e000001"]
    assert sim.journal.event(out["id"])["data"]["text"] == raw


def test_prepared_project_retry_keeps_the_original_version(tmp_path,native_run):
    repo=Repository(tmp_path); job_id="abcd"*8
    payload={"scenario":native_run.scenario.model_dump(mode="json"),"config":native_run.config.model_dump(mode="json"),
             "preparation_records":[],"assistant":{"adopted_prepare_job":job_id}}
    first=repo.save_prepared(payload,job_id)
    assert first==repo.save_prepared(payload,job_id)
    edited=deepcopy(payload);edited["assistant"]={"summary":"后续编辑"}
    repo.save_project(edited,first["id"],1)
    assert repo.save_prepared(payload,job_id)==first
    assert len(repo.list_projects())==1 and repo.get_project(first["id"])["version"]==2


def test_cli_intake_and_resume_share_a_workbench_project(tmp_path):
    from chuantuo.client import prepare_at_workbench
    file=tmp_path/"港口.md";file.write_text(sample_source().text,encoding="utf-8")
    class CountCalls(FixtureGateway):
        def __init__(self): self.calls=0
        def complete(self,*args): self.calls+=1;return super().complete(*args)
    gateway=CountCalls()
    with TestClient(create_app(tmp_path/"store",gateway)) as client:
        client.put("/api/model",json={"model":{"provider":"custom","model":"cli-fixture","base_url":"http://localhost"}}).raise_for_status()
        first=prepare_at_workbench(client,[file],allow_search=False,progress=lambda _:None)
        again=prepare_at_workbench(client,[],resume_job=first["job_id"],progress=lambda _:None)
        assert first==again and first["windows"]==9 and gateway.calls==1
        assert len(client.get("/api/projects").json())==1 and not client.get("/api/runs").json()["runs"]


def test_same_matter_keeps_original_reference_until_content_changes():
    from chuantuo.continuity import Notebook
    from chuantuo.schema import Decision
    book=Notebook()
    def action(status="open",text="第八窗前取得回函"):
        return Decision(text="继续核对",matters=[{"key":"approval","text":text,"status":status}])
    book.record(action(),"e000010",1,[],3)
    book.record(action(),"e000020",2,[],3)
    assert book.open["approval"]["record"]=="e000010"
    book.record(action(text="已取得回函，等待签收"),"e000030",3,[],3)
    assert book.open["approval"]["first_record"]=="e000010" and book.open["approval"]["record"]=="e000030"
    book.record(Decision(text="已经签收",matters=[{"key":"approval","status":"closed"}]),"e000040",4,[],3)
    assert not book.open


def test_markdown_field_table_and_explicit_advanced_fix_do_not_invent_values():
    from chuantuo.source_contract import confirm_named_fields, reconcile
    data=scene_dict(); source=data["sources"][0]
    source["text"]+="\n## 本场状态\n| 主体 | 字段名 | 初始值 |\n| --- | --- | --- |\n| 市议会 | \x60gap\x60 | 2000 |\n| 港务署 | 听证日期 | 未定 |\n## 字段示例\n| 字段 | 单位 |\n| --- | --- |\n| 人气评分 | 点 |"
    reviewed=reconcile({"scenario":Scenario11.model_validate(data).model_dump(mode="json"),"preparation_records":[{}]})
    record=reviewed["preparation_records"][0]; check=record["source_reconciliation"]
    assert check["unresolved_fields"]==["听证日期"] and "人气评分" not in check["named_fields"]
    assert reviewed["scenario"]["entities"][0]["fields"][0]["id"]=="gap"
    reviewed["scenario"]["entities"][1]["fields"].append({"id":"date","label":"听证日期","provenance":{"kind":"user"}})
    record["blocking_questions"].append("另一个未决意图")
    reviewed["assistant"]={"revision_reason":"用户指定听证日期字段，保留未知初值"}
    confirm_named_fields(reviewed)
    assert not check["unresolved_fields"] and record["blocking_questions"]==["另一个未决意图"]


def test_failure_and_retry_restore_exact_private_boundary(tmp_path,native_run):
    class FailAtFour(FixtureGateway):
        def complete(self, config, messages):
            d = json.loads(messages[-1]["content"])
            if d["task"] == "world" and d["window"] == 4:
                raise ModelError("fixture settlement unavailable")
            return super().complete(config,messages)
    directory = tmp_path/"run"
    sim = Simulation(native_run.scenario,native_run.config,FailAtFour(),directory)
    assert sim.run()["status"]=="failed" and sim.window==3
    restored = Simulation.restore(directory,FixtureGateway())
    assert restored.notebooks["engineer"].open["guarantee"]["text"] == CONDITION
    restored.recover(); assert restored.run()["status"] == "completed"
    assert restored.state == native_run.state
    events = restored.journal.selected(transport_details=False)
    assert sum(e["kind"]=="expression" for e in events) == 90
    assert verify_commits(restored.scenario,restored.config,events)["verified_expressions"] == 90
    original = next(e for e in events if e["kind"]=="observation" and e["data"]["actor_id"]=="resident")
    original["data"]["visible"]["self"]["private_note"] = CONDITION
    with pytest.raises(ValueError,match="可见范围"): verify_commits(restored.scenario,restored.config,events)


def test_imported_profile_cannot_select_a_different_key_or_destination(tmp_path,native_run):
    with TestClient(create_app(tmp_path,FixtureGateway())) as client:
        cfg = native_run.config.model_dump(mode="json")
        assert client.put("/api/model",json={"model":cfg["model"]}).status_code == 200
        cfg["model"].update(base_url="https://unselected.example/v1",api_key_env="OPENAI_API_KEY")
        result = client.post("/api/runs",json={"scenario":native_run.scenario.model_dump(mode="json"),"config":cfg})
        assert result.status_code==400 and "本机" in result.json()["detail"]
        assert client.get("/api/runs").json()["runs"] == []


def test_rejected_settlement_reuses_complete_rounds_and_inbox(tmp_path,native_run):
    class BadCitation(FixtureGateway):
        def complete(self, config, messages):
            raw = super().complete(config,messages)
            d = json.loads(messages[-1]["content"])
            if d["task"] == "world" and d["window"] == 1:
                result = json.loads(raw)
                action = next(a for a in d["actions"] if a["actor_id"] == "engineer")
                result["reviews"][0]["influence_refs"] = [{"source":"engineer","target":"council","action_ids":[action["id"]],"explanation":"未列入同一字段"}]
                return json.dumps(result,ensure_ascii=False)
            return raw
    directory = tmp_path/"frozen"
    sim = Simulation(native_run.scenario,native_run.config,BadCitation(),directory)
    assert sim.run()["status"]=="failed" and "action_ids" in sim.error
    ids = [e["id"] for e in sim.journal.snapshot({"expression"})]
    restored = Simulation.restore(directory,FixtureGateway()); restored.recover()
    assert restored.journal.snapshot({"settlement_resume_requested"})[-1]["data"]["frozen_action_ids"] == ids
    assert restored.run()["status"]=="completed" and restored.state==native_run.state
    assert len(restored.journal.snapshot({"expression"}))==90
    assert verify_commits(restored.scenario,restored.config,restored.journal.selected(transport_details=False))["verified_deliveries"]==9
