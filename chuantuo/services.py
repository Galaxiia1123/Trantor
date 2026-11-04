"""Explicit model-assisted preparation and evidence-cited reports."""
from __future__ import annotations

import json
from typing import Literal

from pydantic import Field, ValidationError

from .engine import Simulation, encode
from .models import ModelConfig, ModelGateway
from .schema import Influence, Provenance, Record, RunConfig11, Scenario, Source
from .report_batches import ReportWork
from .preparation_trace import accepted
from .perception import state_sentences
from .observatory import FieldWatch, note_text, observation_lines, observe_field


def parse_scenario(text: str, model: ModelConfig, gateway=None, *, sources: list[Source] | None = None) -> dict:
    if not text.strip():
        raise ValueError("请输入场景材料")
    originals = list(sources or [])
    source_ids = {s.id for s in originals}
    if len(source_ids) != len(originals):
        raise ValueError("材料 ID 不能重复")
    input_id = "input"
    while input_id in source_ids:
        input_id += "_request"
    source = Source(id=input_id, name="用户解析要求" if originals else "用户原始材料", text=text)
    originals.append(source)
    messages = [
        {"role": "system", "content": """你负责把用户材料解析为场景草稿，不能悄悄把猜测当事实。
返回严格匹配 schema 的 JSON。不要创建预设经济/军事领域或资金字段，只提取输入所定义的资料。
引用原始材料用 provenance.kind=material，source_ids 填入 sources 中实际提供的材料 ID。
任何推断必须 provenance.kind=ai_assumption 且说明理由；无法推断的内容列入 missing_information，
缺失状态可显式为 null，不能擅自补零。所有补充也应列在 assumptions。主体、字段、领域资料、
规则、影响边都标明来源。包含结构用 subsystems 的 owner_id 和 entities 的 subsystem_id，
不要把跨层影响边变成包含结构。私有资料默认 visibility.public=false，领域资料不建立 Agent。
缺失权重留 null。仅生成草稿，不启动模拟，不选择模型，不添加 API 密钥。"""},
        {"role": "user", "content": encode({"task": "parse", "text": text, "source": source.model_dump(),
            "sources": [item.model_dump() for item in originals], "schema": Scenario.model_json_schema()})},
    ]
    raw = (gateway or ModelGateway()).complete(model, messages)
    data = json.loads(raw)
    # The original material is preserved byte-for-byte as text independently of
    # the model's attempt to restate or classify it.
    data["sources"] = [item.model_dump() for item in originals]
    unattributed = []
    objects = [*data.get("entities", []), *data.get("influences", []), *data.get("rules", [])]
    for actor in data.get("entities", []):
        objects.extend([*actor.get("fields", []), *actor.get("materials", [])])
    for item in objects:
        provenance = item.get("provenance")
        if not provenance or provenance.get("kind") == "user" or (provenance.get("kind") == "material" and not provenance.get("source_ids")):
            note = "解析模型未提供可核对的来源，暂按 AI 假设保留，需用户审阅。"
            item["provenance"] = {"kind": "ai_assumption", "source_ids": [], "explanation": note}
            unattributed.append(item.get("id", item.get("text", "未标注对象")))
    for edge in data.get("influences", []):
        if edge["provenance"].get("kind") != "material":
            edge["weight"] = None
    if unattributed:
        data.setdefault("assumptions", []).append("未标注来源的对象: " + ", ".join(unattributed))
    scenario = Scenario.model_validate(data)
    return {"scenario": scenario.model_dump(mode="json"), "requires_review": True,
            "record": {"kind": "scene_parse", "model": model.model_dump(mode="json"), "messages": messages, "raw_output": raw}}


class WeightEstimate(Record):
    source: str
    target: str
    weight: float = Field(ge=0, le=1, allow_inf_nan=False)
    reason: str = Field(min_length=1)


class Estimates(Record):
    estimates: list[WeightEstimate]


def estimate_weights(scenario: Scenario, model: ModelConfig, gateway=None, *, defined_only=False) -> dict:
    missing = {(e.source, e.target) for e in scenario.influences if e.weight is None}
    if not missing:
        return {"scenario": scenario.model_dump(mode="json"), "record": {"kind": "weight_estimation", "estimates": []}}
    messages = [
        {"role": "system", "content": "估算缺失的有向边影响强度，范围0到1，不是服从概率，不归一化。只填缺失边，逐边说明理由，不改变用户权重。按 schema 返回 JSON。"},
        {"role": "user", "content": encode({"task": "estimate_weights", "scenario": scenario.world_view(defined_only),
                                            "missing": sorted(missing), "schema": Estimates.model_json_schema()})},
    ]
    raw = (gateway or ModelGateway()).complete(model, messages)
    estimates = Estimates.model_validate_json(raw).estimates
    lookup = {(e.source, e.target): e for e in estimates}
    if len(lookup) != len(estimates) or lookup.keys() != missing:
        raise ValueError("估权结果必须恰好覆盖缺失边，不能覆盖用户设定")
    edges = []
    for edge in scenario.influences:
        if edge.weight is not None:
            edges.append(edge)
            continue
        estimated = lookup[(edge.source, edge.target)]
        edges.append(Influence(source=edge.source, target=edge.target, weight=estimated.weight,
            provenance=Provenance(kind="ai_assumption", explanation=estimated.reason)))
    updated = scenario.model_copy(update={"influences": edges})
    accepted(gateway)
    return {"scenario": updated.model_dump(mode="json"),
            "record": {"kind": "weight_estimation", "model": model.model_dump(mode="json"),
                       "messages": messages, "raw_output": raw, "estimates": [e.model_dump() for e in estimates]}}


class ReportSection(Record):
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    claims: list["ReportClaim"] = Field(default_factory=list)


class ReportClaim(Record):
    basis: Literal["source", "assumption", "simulation", "interpretation", "method"]
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    source_ids: list[str] = Field(default_factory=list)


class Report(Record):
    title: str = Field(min_length=1)
    sections: list[ReportSection] = Field(min_length=1)


def compact_observations(evidence):
    """Retain each reference and its scope; send repeated private observations as deltas."""
    previous = {}
    compact = []
    for event in evidence:
        data = event["data"]
        if event["kind"] == "observation":
            owner, view = data["actor_id"], data["visible"]
            prior_id, prior = previous.get(owner, (None, {}))
            delta = {k:v for k,v in view.items() if k not in {"visible_state","messages","external_events"} and prior.get(k) != v}
            cleared=[k for k in prior if k not in view and k not in {"visible_state","messages","external_events"}]
            if cleared: delta["no_longer_present"]=cleared
            if view.get("external_events"): delta["received_external_events"]=view["external_events"]
            if prior_id:
                delta["continues"] = prior_id
            delta["state"] = {a:{f:v for f,v in values.items() if prior.get("visible_state",{}).get(a,{}).get(f) != v}
                              for a,values in view["visible_state"].items()}
            delta["state"] = {a:values for a,values in delta["state"].items() if values}
            delta["received_actions"] = list(dict.fromkeys(m["action_id"] for m in view["messages"]))
            if copies:=list(dict.fromkeys(m["receipt_id"] for m in view["messages"] if m.get("enclosures"))):
                delta["document_receipts"]=copies
            previous[owner] = (event["id"],view)
            data = {"actor_id":owner,"observation_delta":delta}
        elif event["kind"] == "expression":
            data = {k:v for k,v in data.items() if k != "raw"}
            matters = json.loads(event["data"]["raw"]).get("matters", [])
            if matters:
                data["matters"] = matters
        compact.append({**event,"data":data})
    return compact


def _report_inputs(simulation, questions=None):
    kinds = {"run_created", "scene_parse", "weight_estimation", "observation", "expression",
             "message_delivered", "window_committed", "model_validation_error", "runtime_verification", "run_error",
             "run_status", "run_recovered", "provider_attempt"}
    if isinstance(simulation.config, RunConfig11):
        kinds.update({"settlement_rejected","settlement_resume_requested","experiment_forked","decision_deferred","external_event","message_dispatched","round_resume_requested","agent_result_reused"})
    kinds.update({"pause_goal_set","pause_goal_reached","pause_goal_cancelled","pause_goal_expired"})
    if questions is None and simulation.journal.directory:
        from .repository import Repository
        questions=list(Repository(simulation.journal.directory.parent).field_watches(simulation.journal.directory).values())
    questions=questions or []
    if len(questions)>32:
        raise ValueError("一份报告最多关注 32 个问题")
    with simulation._lock:
        snapshot, events = simulation.report_snapshot(kinds)
        observations=[observe_field(simulation,FieldWatch.model_validate(question),snapshot["window"],details=False,
                                    close_horizon=snapshot["status"] in {"completed","stopped"}) for question in questions]
    discarded = {eid for event in events if event["kind"] == "run_recovered" for eid in event["data"].get("discarded_event_ids", [])}
    # Remove duplicated transport prompts from report input, not from the audit
    # log. All scenario, observations, original expressions and outcomes remain.
    evidence = [e for e in events if e["id"] not in discarded
                and (e["kind"] not in {"provider_attempt","model_validation_error"} or e["data"].get("purpose") in {"agent", "world"})]
    evidence = [{**e, "data": ({k: v for k, v in e["data"].items() if k not in ("messages", "raw_output", "attempts", "source_batches","comparison_ideas")}
                              if e["kind"] in ("scene_parse", "weight_estimation") else e["data"])} for e in evidence]
    if isinstance(simulation.config, RunConfig11):
        evidence = compact_observations(evidence)
    model_id = simulation.config.model_id("report")
    model = simulation.config.catalog[model_id]
    config = simulation.config
    actor_world = {config.model_id("world"), *(config.model_id("actor", d) for d in simulation.depths.values())}
    methods = {key:{"provider":config.catalog[key].provider,"model":config.catalog[key].model} for key in sorted(actor_world)}
    execution = {"actor_world_configs":methods,"report_config":{"provider":model.provider,"model":model.model}}
    if observations:
        execution["observation_questions"]=observations
    if simulation.experiment:
        execution["experiment"] = simulation.experiment
    execution["commits"] = {str(e["window"]):e["id"] for e in evidence if e["kind"] == "window_committed"}
    execution["method_record_ids"] = [e["id"] for e in evidence if e["kind"] in {"run_error","run_status","settlement_rejected","settlement_resume_requested",
                                                                                   "pause_goal_set","pause_goal_reached","pause_goal_cancelled","pause_goal_expired"}]
    messages = [
        {"role": "system", "content": """根据全部证据记录生成中文论文式报告，按 schema 输出 JSON。
开头用一个简短摘要回答本场要探索的问题、实际结果和最关键的转折。后文只展开理解结果所需的内容。
涵盖场景与目标、初始条件及假设、主体及关系、方法、关键事件与状态演化、结果、讨论和局限。
小场景合并相近章节。正文概括与 claims 各有分工，同一事实只完整写一次。关键条件和引用保留，删除泛泛复述。
若提供 observation_questions，它们是操作者关注的问题及引擎按提交记录算出的结果，报告会自动展示。正文重点解释哪些行动、收件或约束影响了这些结果。
每节所有实质结论需有对应 evidence_ids。区分用户材料、AI 估算/假设、模拟事件、你的解释，
重要结论另列 claims。source 引用材料编号及开局或准备记录；assumption 是模拟假设；simulation 只用于窗口内发生的表达、投递、观察或客观结果。
描述结算字段时引用 execution.commits 中对应窗口的提交编号。model 调用、错误、重试、完成状态属于 method，以 provider_attempt、run_status 或拒绝记录为据。
interpretation 用于解释、局限及不能外推的讨论。每条结论按其实际依据填写；同一事实不要在不同章节反复重写。
操作者设置、取消或触发的 pause_goal 记录属于 method。达成目标的字段值另引用 window_committed，作为 simulation 结论。
evidence_ids 只填本次 events 的事件 id（形如 e000001）。材料编号只填在结论的 source_ids；引用文稿的章节用收录该文稿的开局事件作 evidence_ids。客观状态变化引用已提交窗口。带 simulation_ref 的来源是先前推演的快照。
解释使用 basis=interpretation，展示时会自动加上“报告解释”标签，正文无需再重复标签。模拟结果不能直接作为现实结论。失败或未完成的窗口不得写成已提交。
分岔时操作者选择的字段、日期和收件范围属于 method，引用 experiment_forked 或带 experiment 的 run_created。实际到件或状态变化另写 simulation 结论，分别引用相应的 message_delivered 或 window_committed。不要把预定到达日期直接写成已到件。
event_cancel 撤销一项尚未发生的预定事件及其预设通知和赋值，属于方法变化。后续主体自主行动和实际字段结果应分别引用其记录，不能把取消计划本身写成已经发生的主体行动。
如果 experiment.model_history 记录了更换模型，应按每次 starts_window 区分前后的模型。换用模型属于方法变化，不能把随后差异全部归因于场景条件。
experiment.runtime_after 只说明分岔后采用的引擎。分岔前的方法记录以原路线为准。
continuation_condition 表示在原终点之后加入条件继续观察。说明条件的起始窗口和知情范围，并按已提交记录描述后续发展。原条件的同期路线尚未推演时，只能讨论本次演变。
agent_result_reused 含 source 时，表示分岔后首轮采用了原路线已存的判断，属于方法记录，须区别于新的模型调用。按 input_id 去重，注明实际沿用数量。重复实验与首轮沿用是不同的观察方式。
不包含密钥或内部思维链，不补造情节。不能将离线脚本演示称为真实模型实验。
场景虚构与调用方式是两件事。sources 中保留的旧示例运行说明只描述原材料；本次实际调用方式
如有 runtime_verification，依据其网络请求凭证描述。真实 API 调用不等于现实世界实验或有效性验证。
方法简述实际配置与影响结果的异常即可。codex_cli 的 provider_attempt 记录本机订阅 CLI 的调用结果，其成功调用不以 HTTP 状态码为条件。不要把无关的验收项目或未进行的技术检查逐项写成局限。"""},
        {"role": "user", "content": encode({"task": "report", "events": evidence, "status": snapshot, "execution":execution,
                                            "schema": Report.model_json_schema()})},
    ]
    return snapshot,evidence,model_id,model,messages,methods,observations


def _previous_candidate(simulation, messages, model_id, window):
    if not isinstance(simulation.config, RunConfig11):
        return None
    recent = simulation.journal.selected({"report_created","model_validation_error"}, transport_details=False)
    last = next((e for e in reversed(recent) if e["kind"]=="report_created" or e["data"].get("purpose")=="report"), None)
    if not last or last["window"] != window or last["kind"] != "model_validation_error":
        return None
    output = simulation.journal.event(last["data"]["output_id"])
    original = simulation.journal.event(output["data"]["input_id"])
    if (output["kind"]!="report_output" or original["kind"]!="report_input" or original["window"] != window
        or original["data"]["config_id"] != model_id or original["data"]["messages"][0] != messages[0]):
        return None
    old_input = json.loads(original["data"]["messages"][1]["content"])
    old_input.pop("output_correction",None)
    return output if old_input == json.loads(messages[1]["content"]) else None


def plan_report(simulation, reuse=True, questions=None):
    snapshot,evidence,model_id,model,messages,_,observations = _report_inputs(simulation,questions)
    size = sum(len(m["content"]) for m in messages)
    batches = []
    if size > model.context_limit_chars:
        work = ReportWork(simulation,model_id,evidence,snapshot,reuse=reuse,questions=observations)
        batches = work.batches(work.initial(),1)
    hits = sum(batch[3] is not None for batch in batches)
    candidate = _previous_candidate(simulation, messages, model_id, snapshot["window"]) if reuse and not batches else None
    return {"cutoff_window":snapshot["window"],"config_id":model_id,"model":model.model,"provider":model.provider,
            "evidence_count":len(evidence),"input_chars":size,"context_chars":model.context_limit_chars,
            "mode":"batched" if batches else "full","first_level_batches":len(batches),
            "reusable_first_level":hits,"candidate_to_recheck":candidate["id"] if candidate else None,
            "minimum_new_calls":0 if candidate else 1+len(batches)-hits,
            "questions":[row["question"] for row in observations]}


def generate_report(simulation: Simulation, gateway=None, progress=None, *, max_calls=None, reuse=True, expected_window=None, job_id=None, questions=None) -> dict:
    snapshot,evidence,model_id,model,messages,methods,observations = _report_inputs(simulation,questions)
    cutoff_window = snapshot["window"]
    if expected_window is not None and cutoff_window != expected_window:
        raise ValueError("推演窗口已变化，请重新核对报告范围和预算")
    work = ReportWork(simulation,model_id,evidence,snapshot,max_calls,reuse,questions=observations)
    gateway = gateway or simulation.gateway
    ids = {e["id"] for e in evidence}
    by_id = {e["id"]:e for e in evidence}
    sources = {s.id:s for s in simulation.scenario.sources}
    coverage = {"method": "full", "evidence_count": len(evidence)}
    if sum(len(m["content"]) for m in messages) > model.context_limit_chars:
        messages, ids, batch_coverage = work.condense(gateway, messages[0]["content"], Report.model_json_schema(), progress=progress)
        coverage = {"method": "batched", **batch_coverage}
    if progress:
        progress("writing", "证据已整理，正在撰写并校验报告")
    base_messages = messages
    candidate = _previous_candidate(simulation, messages, model_id, cutoff_window) if reuse and coverage["method"]=="full" else None
    for attempt in range(-1 if candidate else 0, 3):
        if attempt == -1:
            output = candidate
            raw = output["data"]["text"]
        else:
            work.charge()
            call = simulation.journal.append("report_input", {"config_id": model_id, "messages": messages}, cutoff_window)
            raw = simulation.call_recorded(model_id, messages, call, gateway)
            output = simulation.journal.append("report_output", {"input_id": call["id"], "text": raw}, cutoff_window)
        try:
            report = Report.model_validate_json(raw)
            native = isinstance(simulation.config, RunConfig11)
            if native and not any(section.claims for section in report.sections):
                raise ValueError("报告至少需要一条注明依据类型与记录的关键结论")
            problems = []
            anchored = []
            creation = next((e for e in evidence if e["kind"]=="run_created" and e["id"] in ids), None)
            original_sources = {s["id"] for s in creation["data"]["scenario"]["sources"]} if creation else set()
            commit_refs = [e["id"] for e in evidence if e["kind"]=="window_committed" and e["id"] in ids]
            def check_event_ids(where, refs):
                missing=set(refs)-ids
                if missing:
                    wrong="、".join(str(r)[:90] for r in sorted(missing)[:4])
                    hint=("。材料编号只可填在结论的 source_ids。原始文稿收录于 "+creation["id"]
                          if creation and missing & original_sources else "。请引用本次提供的原始事件编号")
                    problems.append(where+" 的 evidence_ids 引用了不存在的证据或未提供的事件 "+wrong+hint)
                return not missing
            for section in report.sections:
                check_event_ids("章节「"+section.title+"」",section.evidence_ids)
                for claim in section.claims:
                    where = f"「{section.title}」中的结论“{claim.text[:70]}”"
                    event_ids_ok=check_event_ids(where,claim.evidence_ids)
                    if unknown:=set(claim.source_ids)-sources.keys():
                        problems.append(where+" 的 source_ids 含有未提供的材料编号 "+"、".join(sorted(unknown)[:4]))
                    if not event_ids_ok or unknown:
                        continue
                    records = [by_id[eid] for eid in claim.evidence_ids]
                    if native and claim.basis == "source":
                        if not claim.source_ids:
                            problems.append(where + "缺少 source_ids，请从实际材料中选择：" + "、".join(sources))
                        elif not any(e["kind"] in {"run_created","scene_parse"} for e in records):
                            if creation and set(claim.source_ids) <= original_sources:
                                claim.evidence_ids.append(creation["id"])
                                anchored.append({"section":section.title,"source_ids":claim.source_ids,"anchor":creation["id"]})
                            else:
                                problems.append(where + "缺少能核对所引材料的开局或准备记录")
                    snapshot_source = any(sources[sid].simulation_ref for sid in claim.source_ids) and any(e["kind"] == "run_created" for e in records)
                    if claim.basis == "simulation" and not snapshot_source and not any(e["kind"] in {"window_committed","expression","observation","message_dispatched","message_delivered","external_event"} and e["window"] <= cutoff_window for e in records):
                        if any(e["kind"]=="experiment_forked" or e["kind"]=="run_created" and e["data"].get("experiment") for e in records):
                            dispatch=(simulation.experiment or {}).get("delivery_move") or {}
                            arrivals=[e["id"] for e in evidence if e["id"] in ids and e["kind"]=="message_delivered" and
                                      dispatch.get("dispatch_id") and e["data"].get("dispatch_id")==dispatch["dispatch_id"]]
                            extra="。证明该件实际到达时另写 simulation 结论，引用 "+arrivals[0] if arrivals else ""
                            problems.append(where+"：你引的是操作者的分岔设定。若保留这些依据，此条使用 basis=method，并写“设为”或“预计”"+extra)
                        else:
                            problems.append(where + "：模拟事件结论缺少已提交窗口内的证据。字段结果应引用提交 " + "、".join(commit_refs[-6:]) + "。若实际讨论请求、错误或完成状态，改用 method；若讨论局限，改用 interpretation")
                    if claim.basis == "source" and any(sources[sid].simulation_ref for sid in claim.source_ids):
                        problems.append(where + "的派生项目状态快照必须标注为模拟来源")
                    if claim.basis == "method" and not any(e["kind"] in {"run_created","scene_parse","provider_attempt","model_validation_error","run_error","run_status","runtime_verification","run_recovered","settlement_rejected","settlement_resume_requested","round_resume_requested","agent_result_reused","experiment_forked","decision_deferred",
                        "pause_goal_set","pause_goal_reached","pause_goal_cancelled","pause_goal_expired"} for e in records):
                        problems.append(where + "缺少配置、请求或运行状态记录")
            if problems:
                raise ValueError(" / ".join(problems[:8]))
        except (ValidationError, ValueError) as error:
            issues = ([{key: issue[key] for key in ("type", "loc", "msg")}
                       for issue in error.errors(include_input=False, include_url=False)]
                      if isinstance(error, ValidationError) else [{"msg": str(error)}])
            if attempt>=0:
                simulation.journal.append("model_validation_error", {"purpose": "report", "output_id": output["id"],
                    "attempt": attempt + 1, "issues": issues}, cutoff_window)
            if attempt == 2:
                raise
            payload = json.loads(base_messages[1]["content"])
            payload["output_correction"] = {"previous_response": raw, "issues": issues,
                "instruction": "逐条修正上述分类、原始引用或格式。不同种类的事实分开写，缺少依据则删去该断言。返回完整 JSON。"}
            messages = [base_messages[0], {"role": "user", "content": encode(payload)}]
            if sum(len(m["content"]) for m in messages) > model.context_limit_chars:
                payload["output_correction"].pop("previous_response")
                messages=[base_messages[0],{"role":"user","content":encode(payload)}]
                if sum(len(m["content"]) for m in messages)>model.context_limit_chars:
                    messages=base_messages
        else:
            if attempt == -1:
                coverage["rechecked_output_id"] = output["id"]
            coverage["source_anchors"] = anchored
            break
    lines = [f"# {report.title}", "", "> 这是仿真报告，结果不直接代表现实世界。引用可在完整运行记录中核对。", ""]
    if coverage["method"] == "batched":
        lines.extend([f"> 证据处理使用 {coverage['batches']} 批摘要，覆盖 {len(evidence)} 条原始记录。逐批输入输出保存在运行日志，归纳可能损失细节。", ""])
    cited_ids = {eid for section in report.sections for eid in section.evidence_ids}
    if observations:
        lines.extend(["## 关注问题", "", "以下结果按各问题的期限计算，取自本报告范围内的已提交状态。", ""])
        for row in observations:
            title=row["question"]["title"] or row["actor"]+" · "+row["label"]
            lines.extend(["### "+note_text(title), ""])
            def cite_question(eid, window):
                cited_ids.add(eid)
                return f"[{eid}](#event-{eid})"
            lines.extend(observation_lines(row,cite_question))
            lines.append("")
        coverage["observation_questions"]=observations
    method_labels = "、".join(f"{cfg['provider']} / {cfg['model']}" for cfg in methods.values())
    model_history=(simulation.experiment or {}).get("model_history",[])
    if model_history:
        first=model_history[0]["before"]
        labels=[f"开局沿用 {first['provider']} / {first['model']} 配置。"]
        labels.extend(f"第 {step['starts_window']} 窗起配置为 {step['after']['provider']} / {step['after']['model']}。"
                      for step in model_history)
        method_labels="".join(labels)
        lines.extend([f"> {method_labels}本报告配置为 {model.provider} / {model.model}。", ""])
    else:
        lines.extend([f"> 主体与世界模型配置为 {method_labels}。本报告配置为 {model.provider} / {model.model}。", ""])
    verification = next((e for e in reversed(evidence) if e["kind"] == "runtime_verification"), None)
    if verification:
        receipts = verification["data"]
        successful = receipts.get("http_status_counts", {}).get("200", 0)
        models = receipts.get("actual_response_models", [])
        if successful and models:
            cited_ids.add(verification["id"])
            lines.extend([
                f"> 调用方式核验：成功 HTTP 响应 {successful} 次；接口响应模型：{'、'.join(models)}。"
                f"核验范围以该凭证记录为准；原材料中的历史运行说明不代表本次调用方式。"
                f" [{verification['id']}](#event-{verification['id']})", "",
            ])
    for section in report.sections:
        lines.extend([f"## {section.title}", "", section.text, "",
                      "证据：" + "、".join(f"[{eid}](#event-{eid})" for eid in section.evidence_ids), ""])
        for claim in section.claims:
            label = {"source":"材料所述","assumption":"估算或假设","simulation":"模拟记录","interpretation":"报告解释","method":"运行方法"}[claim.basis]
            refs = "、".join(f"[{eid}](#event-{eid})" for eid in claim.evidence_ids)
            origin = "、".join(sources[sid].name for sid in claim.source_ids)
            lines.extend([f"- 〔{label}〕{claim.text} {refs}" + (f"（{origin}）" if origin else ""), ""])
            cited_ids.update(claim.evidence_ids)
    if isinstance(simulation.config,RunConfig11):
        anchor = next((e for e in reversed(evidence) if e["kind"]=="window_committed"),evidence[0])
        state = snapshot["state"]
        lines.extend(["## 提交状态核对", "",f"以下字段取自第 {cutoff_window} 窗提交的状态。" if cutoff_window else "以下字段取自开局配置。",""])
        summary_count = 0
        for actor in simulation.scenario.entities:
            fields = {f.id:{"label":f.label or f.id,"value":state[actor.id][f.id],"unit":f.unit} for f in actor.fields}
            if fields:
                prose = state_sentences({actor.id:fields})[actor.id]
                lines.append(f"- **{actor.name}**　{prose} [{anchor['id']}](#event-{anchor['id']})")
                summary_count += 1
        if not summary_count:
            lines.append("本场未定义结算字段，可从窗口记录中核对客观后果。")
        lines.append("")
        cited_ids.add(anchor["id"])
    lines.extend(["## 证据索引", "", "完整输入输出保存在运行日志。以下列出被引用的事件类型和窗口。", ""])
    for eid in sorted(cited_ids):
        event = by_id[eid]
        lines.extend([f'<a id="event-{eid}"></a>', f"- {eid}：窗口 {event['window']}，{event['kind']}", ""])
    markdown = "\n".join(lines)
    coverage.update(new_model_calls=work.calls, call_limit=max_calls)
    event = simulation.journal.append("report_created", {"config_id": model_id, "report": report.model_dump(),
        "coverage": coverage, "markdown": markdown, "output_id":output["id"], "job_id":job_id}, cutoff_window)
    if simulation.journal.directory:
        path = simulation.journal.directory / f"report-{event['id']}.md"
        with path.open("x", encoding="utf-8") as stream:
            stream.write(markdown)
    return {"report": report.model_dump(), "markdown": markdown, "record_id": event["id"],
            "cutoff_window": cutoff_window, "coverage": coverage}
