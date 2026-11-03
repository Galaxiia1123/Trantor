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
            delta = {k:v for k,v in view.items() if k not in {"visible_state","messages"} and prior.get(k) != v}
            if prior_id:
                delta["continues"] = prior_id
            delta["state"] = {a:{f:v for f,v in values.items() if prior.get("visible_state",{}).get(a,{}).get(f) != v}
                              for a,values in view["visible_state"].items()}
            delta["state"] = {a:values for a,values in delta["state"].items() if values}
            delta["received_actions"] = list(dict.fromkeys(m["action_id"] for m in view["messages"]))
            previous[owner] = (event["id"],view)
            data = {"actor_id":owner,"observation_delta":delta}
        elif event["kind"] == "expression":
            data = {k:v for k,v in data.items() if k != "raw"}
            matters = json.loads(event["data"]["raw"]).get("matters", [])
            if matters:
                data["matters"] = matters
        compact.append({**event,"data":data})
    return compact


def _report_inputs(simulation):
    kinds = {"run_created", "scene_parse", "weight_estimation", "observation", "expression",
             "message_delivered", "window_committed", "model_validation_error", "runtime_verification", "run_error",
             "run_status", "run_recovered", "provider_attempt"}
    if isinstance(simulation.config, RunConfig11):
        kinds.update({"settlement_rejected","settlement_resume_requested"})
    snapshot, events = simulation.report_snapshot(kinds)
    discarded = {eid for event in events if event["kind"] == "run_recovered" for eid in event["data"].get("discarded_event_ids", [])}
    # Remove duplicated transport prompts from report input, not from the audit
    # log. All scenario, observations, original expressions and outcomes remain.
    evidence = [e for e in events if e["id"] not in discarded
                and (e["kind"] not in {"provider_attempt","model_validation_error"} or e["data"].get("purpose") in {"agent", "world"})]
    evidence = [{**e, "data": ({k: v for k, v in e["data"].items() if k not in ("messages", "raw_output", "attempts", "source_batches")}
                              if e["kind"] in ("scene_parse", "weight_estimation") else e["data"])} for e in evidence]
    if isinstance(simulation.config, RunConfig11):
        evidence = compact_observations(evidence)
    model_id = simulation.config.model_id("report")
    model = simulation.config.catalog[model_id]
    config = simulation.config
    actor_world = {config.model_id("world"), *(config.model_id("actor", d) for d in simulation.depths.values())}
    methods = {key:{"provider":config.catalog[key].provider,"model":config.catalog[key].model} for key in sorted(actor_world)}
    execution = {"actor_world_configs":methods,"report_config":{"provider":model.provider,"model":model.model}}
    execution["commits"] = {str(e["window"]):e["id"] for e in evidence if e["kind"] == "window_committed"}
    execution["method_record_ids"] = [e["id"] for e in evidence if e["kind"] in {"run_error","run_status","settlement_rejected","settlement_resume_requested"}]
    messages = [
        {"role": "system", "content": """根据全部证据记录生成中文论文式报告，按 schema 输出 JSON。
涵盖场景与目标、初始条件及假设、主体及关系、方法、关键事件与状态演化、结果、讨论和局限。
每节所有实质结论需有对应 evidence_ids。区分用户材料、AI 估算/假设、模拟事件、你的解释，
重要结论另列 claims。source 引用材料编号及开局或准备记录；assumption 是模拟假设；simulation 只用于窗口内发生的表达、投递、观察或客观结果。
描述结算字段时引用 execution.commits 中对应窗口的提交编号。model 调用、错误、重试、完成状态属于 method，以 provider_attempt、run_status 或拒绝记录为据。
interpretation 用于解释、局限及不能外推的讨论。每条结论按其实际依据填写；同一事实不要在不同章节反复重写。
source_ids 只填写实际使用的材料编号。客观状态变化引用已提交窗口。带 simulation_ref 的来源是先前推演的快照。
对解释明确标注“报告解释”。模拟结果不能直接作为现实结论。失败或未完成的窗口不得写成已提交。
不包含密钥或内部思维链，不补造情节。不能将离线脚本演示称为真实模型实验。
场景虚构与调用方式是两件事。sources 中保留的旧示例运行说明只描述原材料；本次实际调用方式
如有 runtime_verification，依据其网络请求凭证描述。真实 API 调用不等于现实世界实验或有效性验证。"""},
        {"role": "user", "content": encode({"task": "report", "events": evidence, "status": snapshot, "execution":execution,
                                            "schema": Report.model_json_schema()})},
    ]
    return snapshot,evidence,model_id,model,messages,methods


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


def plan_report(simulation, reuse=True):
    snapshot,evidence,model_id,model,messages,_ = _report_inputs(simulation)
    size = sum(len(m["content"]) for m in messages)
    batches = []
    if size > model.context_limit_chars:
        work = ReportWork(simulation,model_id,evidence,snapshot,reuse=reuse)
        batches = work.batches(work.initial(),1)
    hits = sum(batch[3] is not None for batch in batches)
    candidate = _previous_candidate(simulation, messages, model_id, snapshot["window"]) if reuse and not batches else None
    return {"cutoff_window":snapshot["window"],"config_id":model_id,"model":model.model,"provider":model.provider,
            "evidence_count":len(evidence),"input_chars":size,"context_chars":model.context_limit_chars,
            "mode":"batched" if batches else "full","first_level_batches":len(batches),
            "reusable_first_level":hits,"candidate_to_recheck":candidate["id"] if candidate else None,
            "minimum_new_calls":0 if candidate else 1+len(batches)-hits}


def generate_report(simulation: Simulation, gateway=None, progress=None, *, max_calls=None, reuse=True, expected_window=None, job_id=None) -> dict:
    snapshot,evidence,model_id,model,messages,methods = _report_inputs(simulation)
    cutoff_window = snapshot["window"]
    if expected_window is not None and cutoff_window != expected_window:
        raise ValueError("推演窗口已变化，请重新核对报告范围和预算")
    work = ReportWork(simulation,model_id,evidence,snapshot,max_calls,reuse)
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
            if any(not set(section.evidence_ids) <= ids for section in report.sections):
                raise ValueError("报告引用了不存在的证据或本次未提供的记录")
            problems = []
            anchored = []
            creation = next((e for e in evidence if e["kind"]=="run_created" and e["id"] in ids), None)
            original_sources = {s["id"] for s in creation["data"]["scenario"]["sources"]} if creation else set()
            commit_refs = [e["id"] for e in evidence if e["kind"]=="window_committed" and e["id"] in ids]
            for section in report.sections:
                for claim in section.claims:
                    where = f"「{section.title}」中的结论“{claim.text[:70]}”"
                    if not set(claim.evidence_ids) <= ids or not set(claim.source_ids) <= sources.keys():
                        problems.append(where + "引用了不存在的证据或材料")
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
                    if claim.basis == "simulation" and not snapshot_source and not any(e["kind"] in {"window_committed","expression","observation","message_delivered"} and e["window"] <= cutoff_window for e in records):
                        problems.append(where + "：模拟事件结论缺少已提交窗口内的证据。字段结果应引用提交 " + "、".join(commit_refs[-6:]) + "。若实际讨论请求、错误或完成状态，改用 method；若讨论局限，改用 interpretation")
                    if claim.basis == "source" and any(sources[sid].simulation_ref for sid in claim.source_ids):
                        problems.append(where + "的派生项目状态快照必须标注为模拟来源")
                    if claim.basis == "method" and not any(e["kind"] in {"run_created","scene_parse","provider_attempt","model_validation_error","run_error","run_status","runtime_verification","run_recovered","settlement_rejected","settlement_resume_requested"} for e in records):
                        problems.append(where + "缺少配置、请求或运行状态记录")
            if problems:
                raise ValueError(" / ".join(problems[:8]))
        except (ValidationError, ValueError) as error:
            if attempt == -1:
                continue
            issues = ([{key: issue[key] for key in ("type", "loc", "msg")}
                       for issue in error.errors(include_input=False, include_url=False)]
                      if isinstance(error, ValidationError) else [{"msg": str(error)}])
            simulation.journal.append("model_validation_error", {"purpose": "report", "output_id": output["id"],
                "attempt": attempt + 1, "issues": issues}, cutoff_window)
            if attempt == 2:
                raise
            payload = json.loads(base_messages[1]["content"])
            payload["output_correction"] = {"previous_response": raw, "issues": issues,
                "instruction": "逐条修正上述分类、原始引用或格式。不同种类的事实分开写，缺少依据则删去该断言。返回完整 JSON。"}
            messages = [base_messages[0], {"role": "user", "content": encode(payload)}]
            if sum(len(m["content"]) for m in messages) > model.context_limit_chars:
                messages = base_messages
        else:
            if attempt == -1:
                coverage["rechecked_output_id"] = output["id"]
            coverage["source_anchors"] = anchored
            break
    lines = [f"# {report.title}", "", "> 这是仿真报告，结果不直接代表现实世界。引用可在完整运行记录中核对。", ""]
    if coverage["method"] == "batched":
        lines.extend([f"> 证据处理使用 {coverage['batches']} 批摘要，覆盖 {len(evidence)} 条原始记录。逐批输入输出保存在运行日志，归纳可能损失细节。", ""])
    cited_ids = {eid for section in report.sections for eid in section.evidence_ids}
    method_labels = "、".join(f"{cfg['provider']} / {cfg['model']}" for cfg in methods.values())
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
