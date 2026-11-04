"""Prepare a new scenario at a committed boundary and compare unit-compatible field values."""
from __future__ import annotations

import hashlib
import json
import math
from copy import deepcopy
from decimal import Decimal
from pathlib import Path
from typing import Literal

from .engine import Simulation, encode
from pydantic import Field, model_validator

from .models import ModelConfig, protect_credentials
from .mail_timing import seal
from .schema import Identifier, Provenance, Record, RouteTiming, RunConfig11, Scenario11, ScheduledValue, Source, StateField, load_scenario


def boundary(simulation, window=None):
    with simulation._lock:
        selected = simulation.window if window is None else window
        if not 0 <= selected <= simulation.window:
            raise ValueError("只能选择开局或已经提交的窗口")
        if selected == 0:
            return {"window":0,"event_id":"e000001","state":simulation._scenario.initial_state()}
        commit = simulation.journal.settlement(selected)
        return {"window":selected,"event_id":commit["id"],"state":deepcopy(commit["data"]["state"])}


def branch_payload(simulation, window, title):
    selected = boundary(simulation,window)
    scene = simulation.scenario
    marker = {"run_id":simulation.run_id, "window":window, "event_id":selected["event_id"],
              "state_sha256":hashlib.sha256(encode(selected["state"]).encode("utf-8")).hexdigest()}
    source_id = "snapshot_" + simulation.run_id + "_" + str(window)
    ids = {s.id for s in scene.sources}
    while source_id in ids:
        source_id += "_copy"
    material = Source(id=source_id, name=f"模拟状态快照 · {scene.title} · 窗口 {window}",
                      text=encode({**marker,"state":selected["state"]}), simulation_ref=marker)
    entities = []
    for actor in scene.entities:
        fields = []
        for field in actor.fields:
            value = selected["state"][actor.id][field.id]
            if window:
                provenance = Provenance(kind="material",source_ids=[source_id],
                    explanation=f"取自模拟运行 {simulation.run_id} 的已提交窗口 {window}，字段 {actor.id}.{field.id}。")
                field = field.model_copy(update={"value":value,"provenance":provenance})
            fields.append(field)
        entities.append(actor.model_copy(update={"fields":fields}))
    label = (title or "").strip() or f"{scene.title} · 从窗口 {window} 派生"
    assumptions = [*scene.assumptions, f"初始状态取自运行 {simulation.run_id} 的窗口 {window}。新主体从空白记忆开始，历史收件和承诺可由用户另行定义。"]
    revised = scene.model_copy(update={"title":label,"entities":entities,"sources":[*scene.sources,material],"assumptions":assumptions})
    if isinstance(revised,Scenario11):
        if window:
            revised=revised.model_copy(update={"assumptions":[*revised.assumptions,
                f"新开局第 1 窗对应原路线第 {window+1} 窗。结构化的规则有效期、线路时效版本、事件和字段转入期限按这一对应关系换算。"]})
        rules=[rule.model_copy(update={"active_from":max(1,rule.active_from-window),
                "active_until":rule.active_until-window if rule.active_until else None}) for rule in revised.rules
               if rule.active_until is None or rule.active_until>window]
        routes={}
        for route in sorted(revised.mail_routes,key=lambda r:r.active_from):
            start=max(1,route.active_from-window)
            routes[route.source,route.target,start]=route.model_copy(update={"active_from":start})
        entities=[]
        for actor in revised.entities:
            fields=[]
            for f in actor.fields:
                if window and f.constraints and f.constraints.entry_windows:
                    periods=[]
                    for p in f.constraints.entry_windows:
                        closed=p.closed_before_start or p.last is not None and p.last<=window
                        period=p.model_copy(update={"closed_before_start":True}) if closed else p.model_copy(update={
                            "first":max(1,p.first-window),"last":p.last-window if p.last else None})
                        periods.append(period)
                    f=f.model_copy(update={"constraints":f.constraints.model_copy(update={"entry_windows":periods})})
                fields.append(f)
            entities.append(actor.model_copy(update={"fields":fields}))
        revised = revised.model_copy(update={"rules":rules,"mail_routes":list(routes.values()),"entities":entities,
            "scheduled_events":[event.model_copy(update={"window":event.window-window}) for event in revised.scheduled_events if event.window>window]})
    return {"scenario":revised.model_dump(mode="json"), "config":simulation.config.model_dump(mode="json"),
            "preparation_records":[], "assistant":{}}


class Adoption(Record):
    window: int = Field(gt=0)
    suggestion_index: int = Field(ge=0)
    entity_id: Identifier
    field: StateField
    title: str = Field(default="",max_length=250)


def adopt_suggestion(simulation, request: Adoption):
    selected = boundary(simulation,request.window)
    event = simulation.journal.event(selected["event_id"])
    suggestions = event["data"]["suggested_fields"]
    if request.suggestion_index >= len(suggestions):
        raise ValueError("该窗口不存在这条字段建议")
    draft = branch_payload(simulation,request.window,request.title)
    scene = draft["scenario"]
    actor = next((a for a in scene["entities"] if a["id"] == request.entity_id),None)
    if actor is None or request.field.domain not in scene["domains"]:
        raise ValueError("请选择已定义的主体和领域")
    if request.field.id in {f["id"] for f in actor["fields"]}:
        raise ValueError("主体已有同编号字段")
    source = scene["sources"][-1]
    proposal = suggestions[request.suggestion_index]
    source["text"] += "\n" + encode({"suggestion_index":request.suggestion_index,"world_suggestion":proposal,
                                     "accepted_field":request.field.id, "actor_id":request.entity_id})
    provenance = Provenance(kind="user", source_ids=[source["id"]],
        explanation=f"用户在新项目中采纳窗口 {request.window}、记录 {event['id']} 的字段建议，并指定类型、初值与可见性。世界原文：{proposal}")
    actor["fields"].append(request.field.model_copy(update={"provenance":provenance}).model_dump(mode="json"))
    draft["scenario"] = load_scenario(scene).model_dump(mode="json")
    return draft


def compare(left, right, left_window=None, right_window=None):
    a, b = boundary(left,left_window), boundary(right,right_window)
    def fields(sim):
        return {(actor.id,f.id):(actor.name,f) for actor in sim.scenario.entities for f in actor.fields}
    ldefs, rdefs = fields(left), fields(right)
    rows = []
    for key in sorted(ldefs.keys() | rdefs.keys()):
        owner, name = key
        def describe(definitions, snap):
            if key not in definitions:
                return None
            actor, field = definitions[key]
            return {"actor_name":actor,"label":field.label or field.id, "domain":field.domain,"kind":field.kind,"unit":field.unit,
                    "value":snap["state"][owner][name]}
        lvalue, rvalue = describe(ldefs,a), describe(rdefs,b)
        delta = None
        delta_text = None
        if lvalue is None or rvalue is None:
            status = "undefined"
        elif any(lvalue[k] != rvalue[k] for k in ("kind","domain","unit")):
            status = "incompatible"
        elif lvalue["value"] is None or rvalue["value"] is None:
            status = "unknown"
        else:
            status = "equal" if lvalue["value"] == rvalue["value"] else "different"
            if lvalue["kind"] == "number":
                left_num,right_num = lvalue["value"],rvalue["value"]
                if type(left_num) is int and type(right_num) is int:
                    delta = right_num-left_num
                    delta_text = str(delta)
                else:
                    difference = Decimal(str(right_num)) - Decimal(str(left_num))
                    approximate = float(difference)
                    delta = approximate if math.isfinite(approximate) else None
                    delta_text = str(difference.normalize()) if difference else "0"
        rows.append({"actor_id":owner, "field_id":name, "left":lvalue, "right":rvalue, "status":status,"delta":delta,"delta_text":delta_text})
    def head(sim, snap):
        return {"run_id":sim.run_id,"title":sim.scenario.title,"window":snap["window"],"event_id":snap["event_id"],
                "duration":sim.config.window_duration}
    return {"left":head(left,a), "right":head(right,b),"rows":rows,
            "note":"按主体和字段编号对齐，单位、类型与领域一致时才计算数值差。比较反映所选快照，不能归因于某个单一设定。"}


class EventMove(Record):
    event_id: Identifier
    new_window: int | None = Field(default=None,ge=1,strict=True)


class DeliveryMove(Record):
    dispatch_id: str = Field(pattern=r"^e[0-9]{6,12}$")
    new_window: int = Field(ge=1,le=1_000_000,strict=True)


class ExperimentRequest(Record):
    window: int = Field(ge=0)
    through: int = Field(ge=1)
    kind: Literal["condition","field","opening","transit","delivery_time","event_time","event_cancel","repeat","extend","model_continue"] = "condition"
    extension_limit: int | None = Field(default=None,ge=1,le=1_000_000,strict=True)
    ends_window: int | None = Field(default=None,ge=1,strict=True)
    condition: str = Field(default="", max_length=1600)
    assignment: ScheduledValue | None = None
    route: RouteTiming | None = None
    event_move: EventMove | None = None
    delivery_move: DeliveryMove | None = None
    audience: Literal["public", "readers", "world"] = "world"
    readers: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.through <= self.window:
            raise ValueError("终点须晚于分岔窗口")
        if self.ends_window is not None and (self.kind!="condition" or self.ends_window<=self.window):
            raise ValueError("只有文本条件可设最后有效窗口，且须在分岔点之后")
        if (self.kind in {"extend","model_continue"})!=(self.extension_limit is not None):
            raise ValueError("延长观察需要为新增窗口设定单独的调用上限")
        if self.kind in {"extend","model_continue"} and (self.through-self.window>100 or self.condition.strip() or self.audience!="world"):
            raise ValueError("每次可继续 1 至 100 窗，沿用原条件和知情范围")
        if (self.kind=="condition" and not self.condition.strip()) or (self.kind=="repeat" and self.condition.strip()):
            raise ValueError("改变条件时请填写具体情境，原样重跑时请留空")
        if (self.kind in {"field","opening"})!=(self.assignment is not None):
            raise ValueError("字段实验只能选择一个已定义的字段和目标值")
        if self.kind=="opening" and (self.window!=0 or self.audience!="world" or self.condition.strip()):
            raise ValueError("改变开局初值须从开局分岔，并沿用该字段原有的可见范围")
        if (self.kind=="transit")!=(self.route is not None):
            raise ValueError("线路实验需要一个发送方、一个收件方和送达时间")
        if (self.kind=="delivery_time")!=(self.delivery_move is not None):
            raise ValueError("单封信改期需要已寄出的发件凭据和新的到达窗口")
        if self.delivery_move and (self.delivery_move.new_window<=self.window or self.audience!="world" or self.condition.strip()):
            raise ValueError("单封信的新到达日期须在分岔之后。发件正文和收件人沿用原信")
        if self.kind=="transit" and (self.audience!="world" or self.condition.strip()):
            raise ValueError("线路实验只改变指定方向的通信时效，时效由通信双方知晓")
        if (self.kind in {"event_time","event_cancel"})!=(self.event_move is not None):
            raise ValueError("事件日期实验需要选择一个日历事件及新的发生窗口")
        if self.kind=="event_time" and (self.audience!="world" or self.condition.strip() or self.event_move.new_window is None or self.event_move.new_window<=self.window):
            raise ValueError("新日期必须晚于分岔点，原事件的内容与通知范围沿用原设定")
        if self.kind=="event_cancel" and (self.event_move.new_window is not None or self.audience!="world" or self.condition.strip()):
            raise ValueError("取消预定事件时，新的日期须为空，主体继续依据此前的所知行动")
        if (self.audience == "readers") != bool(self.readers) or len(set(self.readers)) != len(self.readers):
            raise ValueError("请选择实际获知条件的主体")
        if self.kind == "repeat" and self.audience != "world":
            raise ValueError("原样重跑无需指定知情范围")
        return self


def prepare_experiment(parent, spec, *, allow_same_field=False):
    """Validate the intervention before scheduling any model work."""
    if not isinstance(parent.config, RunConfig11):
        raise ValueError("历史版本的场景可另存开局，条件对照需要 v1.1 场景")
    if spec.kind=="model_continue":
        with parent._lock:
            if parent.status not in {"paused","failed","interrupted","completed","stopped"} or spec.window!=parent.window:
                raise ValueError("请从已暂停或已结束路线的最后一次提交位置继续")
            return boundary(parent,spec.window),parent.scenario,f"从第 {spec.window} 窗换用模型，继续到第 {spec.through} 窗"
    if spec.kind=="extend":
        with parent._lock:
            if parent.status not in {"completed","stopped"} or spec.window!=parent.window or parent.window<1:
                raise ValueError("请从已经结束的路线最后一次结算继续观察")
            return boundary(parent,spec.window),parent.scenario,f"从第 {spec.window} 窗继续观察到第 {spec.through} 窗"
    if spec.through > parent.window:
        raise ValueError("请选择原路线已经提交的终点")
    if parent.experiment and spec.window < parent.experiment["window"]:
        raise ValueError("请打开原路线以选择更早的分岔窗口")
    selected = boundary(parent, spec.window)
    if spec.kind=="delivery_time":
        mail,old=_move_in_transit(parent,spec)
        names={a.id:a.name for a in parent._scenario.entities}
        text=f"{names[mail['data']['from']]}寄给{names[mail['data']['to']]}的这一封信从第 {old} 窗改至第 {spec.delivery_move.new_window} 窗送达"
        return selected,parent.scenario,text
    scene,condition=apply_intervention(parent._scenario,spec,parent.run_id,selected["state"],parent.config.catalog.values(),allow_same_field=allow_same_field)
    return selected,scene,condition


def _move_in_transit(parent,spec):
    mail,old=in_transit_at(parent,spec.delivery_move.dispatch_id,spec.window)
    new=spec.delivery_move.new_window
    if old==new or min(old,new)>spec.through:
        raise ValueError("新旧日期应不同，且至少有一个落在比较时段内")
    return mail,old


def in_transit_at(parent,key,window):
    try:
        mail=parent.journal.event(key)
    except FileNotFoundError:
        raise ValueError("没有这封信的发件凭据") from None
    if not 0<=window<=parent.window or mail["kind"]!="message_dispatched" or key in parent._discarded or mail["window"]>window:
        raise ValueError("只能改期分岔时已经寄出、仍在途的这封信")
    old=parent.mail_timing.due(mail,window)
    if old<=window or parent.journal.arrival(key,window,parent._discarded):
        raise ValueError("这封信在分岔时已经送达，请选择更早的窗口或另一封在途信")
    return mail,old


def apply_intervention(baseline,spec,source_id,state=None,configs=(),*,allow_same_field=False):
    """Use the same scenario contract for a proposed question and an actual fork."""
    if spec.kind in {"extend","model_continue"}:
        raise ValueError("延长观察沿用已结束路线的原条件")
    if spec.kind=="delivery_time":
        raise ValueError("请从具体路线中选择分岔时仍在途的发件凭据")
    scene = baseline.model_dump(mode="json")
    condition = spec.condition.strip()
    if spec.kind in {"field","opening"}:
        change=spec.assignment
        actor=next((a for a in baseline.entities if a.id==change.entity_id),None)
        f=next((f for f in actor.fields if f.id==change.field_id),None) if actor else None
        if not f or not f.permits(change.value):
            raise ValueError("请选择可以调整的已定义字段，并符合它的类型、范围和约束")
        if spec.kind=="field" and f.constraints and f.constraints.fixed:
            raise ValueError("该字段在运行中固定。若要从第一轮就采用另一种取值，请选择“改变开局初值”")
        period=f.entry_period(change.value)
        if spec.kind=="opening" and period and change.value!=f.value:
            raise ValueError("该取值需要在运行中的指定窗口转入，不能作为新的开局初值")
        if spec.kind=="field" and period and not period.allows(spec.window+1):
            raise ValueError("分岔后的首个窗口不在该目标值允许转入的期限内")
        if not allow_same_field and state is not None and state[actor.id][f.id]==change.value:
            raise ValueError("目标值与分岔时相同，可选择原样重跑")
        value="未知" if change.value is None else ("是" if change.value else "否") if isinstance(change.value,bool) else str(change.value)+(f.unit or "")
        if spec.kind=="opening":
            target=next(item for a in scene["entities"] if a["id"]==actor.id for item in a["fields"] if item["id"]==f.id)
            target["value"]=change.value
            target["provenance"]={"kind":"user","explanation":"操作者在对照路线设定的开局初值，沿用原定义、约束和可见范围。"}
            condition=f"{actor.name}的{f.label or f.id}从开局设为{value}"
        else:
            event_key=hashlib.sha256(encode([source_id,spec.model_dump(mode="json",exclude_none=True)]).encode()).hexdigest()[:12]
            scene["scheduled_events"].append({"id":"variation_"+event_key,"window":spec.window+1,
                "text":condition or "本窗有一项外部调整，将在结算时生效。",
                "visibility":{"public":spec.audience=="public","readers":spec.readers},
                "assignments":[change.model_dump(mode="json")],"provenance":{"kind":"user","explanation":"操作者在字段对照中指定。"}})
            condition=condition or f"{actor.name}的{f.label or f.id}在第 {spec.window+1} 窗末设为{value}"
    elif spec.kind=="transit":
        r=spec.route
        names={a.id:a.name for a in baseline.entities}
        if not {r.source,r.target}<=names.keys() or r.source==r.target:
            raise ValueError("请选择不同的发送方和收件方")
        start=spec.window+1
        prior=max((p for p in baseline.mail_routes if (p.source,p.target)==(r.source,r.target) and p.active_from<=start),
                  key=lambda p:p.active_from,default=None)
        if r.transit_windows==(prior.transit_windows if prior else 0):
            raise ValueError("指定方向原本就是这个时效，可选择原样重跑")
        scene["mail_routes"]=[old for old in scene["mail_routes"] if (old["source"],old["target"],old["active_from"])!=(r.source,r.target,start)]
        scene["mail_routes"].append({**r.model_dump(),"active_from":start,
            "provenance":{"kind":"user","explanation":"操作者在对照实验中指定新发件的送达时效。"}})
        timing=f"发出后第 {r.transit_windows} 窗的窗首送达" if r.transit_windows else "发出当轮结束时送达"
        condition=f"{names[r.source]}发给{names[r.target]}的新消息，{timing}"
    elif spec.kind in {"event_time","event_cancel"}:
        move=spec.event_move
        event=next((item for item in scene["scheduled_events"] if item["id"]==move.event_id),None)
        if not event or event["window"]<=spec.window:
            raise ValueError("请选择在分岔时尚未发生的已定义事件")
        original=event["window"]
        if spec.kind=="event_cancel":
            if original>spec.through:
                raise ValueError("请把比较终点选到原定事件所在窗口或之后")
            scene["scheduled_events"].remove(event)
            condition=f"取消原定第 {original} 窗的“{event['text'][:90]}”"
            scene["rules"].append({
                "text":f"本路线取消原定第 {original} 窗的外部事件 {event['id']}（{event['text']}）。"
                       "该事件的预设通知和预设字段赋值一并撤销。若主体采取其他行动，请根据实际行动另行判断后果。",
                "active_from":spec.window+1,"visibility":{"public":False,"readers":[]},
                "provenance":{"kind":"user","explanation":"操作者取消尚未发生的日历事件，作为本路线的客观条件。"},
            })
        else:
            if original==move.new_window or min(original,move.new_window)>spec.through:
                raise ValueError("两个日期应不同，并至少有一个落在观察区间内")
            event["window"]=move.new_window
            event["provenance"]={"kind":"user","explanation":f"对照实验将原定第 {original} 窗的事件改至第 {move.new_window} 窗，沿用原事件内容和通知范围。"}
            condition=f"把“{event['text'][:90]}”从第 {original} 窗改到第 {move.new_window} 窗"
    elif condition:
        scene["rules"].append({"text":condition, "active_from":spec.window + 1,"active_until":spec.ends_window,
            "visibility":{"public":spec.audience == "public","readers":spec.readers},
            "provenance":{"kind":"user","explanation":f"对照实验设定，自第 {spec.window + 1} 窗起生效。"}})
    scene = Scenario11.model_validate(scene)
    protect_credentials(scene.model_dump(mode="json"), configs)
    return scene, condition


def fork_experiment(parent, spec, run_id, job_id, directory: Path, *, study=None, call_limit=None, allow_same_field=False, continuation_model=None):
    """Copy a sealed prefix once. Stable event positions preserve private inboxes and citations."""
    with parent._lock:
        selected, scene, condition = prepare_experiment(parent,spec,allow_same_field=allow_same_field)
        inherited_goals=parent.snapshot().get("pause_goals",[]) if spec.kind=="model_continue" else []
    if (spec.kind=="model_continue")!=(continuation_model is not None):
        raise ValueError("换用模型继续时需要明确指定后续模型")
    config = parent.config.model_copy(update={"windows":spec.through})
    if continuation_model is not None:
        continuation_model=ModelConfig.model_validate(continuation_model)
        if continuation_model==parent.config.model or continuation_model.provider=="demo":
            raise ValueError("请选择与原路线不同的实际模型配置")
        config=config.model_copy(update={"model":continuation_model})
        condition=f"第 {spec.window+1} 窗起采用 {continuation_model.model}，继续至第 {spec.through} 窗"
    if spec.kind in {"extend","model_continue"}:
        config=config.model_copy(update={"model_call_limit":spec.extension_limit})
    elif call_limit is not None:
        limits=[v for v in (config.model_call_limit,call_limit) if v is not None]
        config=config.model_copy(update={"model_call_limit":min(limits)})
    marker = {**spec.model_dump(mode="json"), "condition":condition, "parent_id":parent.run_id,
              "parent_event":selected["event_id"],
              "state_sha256":hashlib.sha256(encode(selected["state"]).encode()).hexdigest()}
    marker["runtime_after"]={"from_window":spec.window+1,"engine":"trantor-local-v1"}
    model_history=deepcopy((parent.experiment or {}).get("model_history",[]))
    if continuation_model is not None:
        fields={"provider","model","thinking","max_tokens","context_limit_chars"}
        marker["model_change"]={"starts_window":spec.window+1,
                                "before":parent.config.model.model_dump(mode="json",include=fields),
                                "after":continuation_model.model_dump(mode="json",include=fields)}
        model_history.append(marker["model_change"])
    if model_history:
        marker["model_history"]=model_history
    if study:
        marker["study"]=study
    timing=list((parent.experiment or {}).get("mail_timing",()))
    if spec.kind=="delivery_time":
        mail,old=_move_in_transit(parent,spec)
        timing.append({"dispatch":mail["id"],"seal":seal(mail),"after":spec.window,"old":old,"new":spec.delivery_move.new_window})
    if timing:
        marker["mail_timing"]=timing
    expected=selected["state"]
    if spec.kind=="opening":
        change=spec.assignment
        marker["opening_change"]={"before":expected[change.entity_id][change.field_id],"after":change.value}
        expected=deepcopy(expected)
        expected[change.entity_id][change.field_id]=change.value
        marker["opening_state_sha256"]=hashlib.sha256(encode(expected).encode()).hexdigest()
    cancelled=deepcopy((parent.experiment or {}).get("cancelled_events",[]))
    if spec.kind in {"event_time","event_cancel"}:
        source_event=next(e for e in parent._scenario.scheduled_events if e.id==spec.event_move.event_id)
        marker["original_event"]={"id":source_event.id,"window":source_event.window,"provenance":source_event.provenance.model_dump(mode="json")}
        if source_event.window<=parent.window:
            receipts,_=parent.journal.window_rows(source_event.window,0,kind="external_event")
            marker["original_event"]["record"]=next((e["id"] for e in receipts if e["data"]["id"]==source_event.id and e["id"] not in parent._discarded),None)
        if spec.kind=="event_cancel":
            cancelled.append({"event":source_event.model_dump(mode="json"),"after_window":spec.window,"source_run":parent.run_id})
    if cancelled:
        marker["cancelled_events"]=cancelled
    directory.mkdir(parents=True, exist_ok=False)
    cutoff = int(selected["event_id"][1:])
    with (parent.journal.directory/"events.jsonl").open("rb") as source, (directory/"events.jsonl").open("xb") as dest:
        for index in range(1, cutoff + 1):
            raw = source.readline()
            if not raw.endswith(b"\n"):
                raise ValueError("原路线的已提交前缀不完整")
            event = json.loads(raw)
            if event["id"] != f"e{index:06d}":
                raise ValueError("原路线的证据顺序已改变")
            kind = event["kind"]
            if index == 1:
                event["data"].update(run_id=run_id, launch_job_id=job_id, scenario=scene.model_dump(mode="json"),
                                     config=config.model_dump(mode="json"), experiment=marker)
            elif (kind.startswith(("report_", "analysis_")) or kind in
                  {"project_linked","provider_attempt","prepare_provider_attempt","runtime_verification"}):
                # Historical usage and post-run analyses belong to the parent. Preserve the slot for links.
                event.update(kind="inherited_reference", data={"source_run":parent.run_id,"source_kind":kind})
                event.pop("_packed", None)
            else:
                dest.write(raw)
                continue
            dest.write((encode(event) + "\n").encode())
    alternate = Simulation.restore(directory, gateway=parent.gateway)
    if alternate.window != spec.window or alternate.state != expected:
        raise ValueError("分岔状态与原路线提交不一致")
    alternate.journal.append("experiment_forked",marker,spec.window)
    for goal in inherited_goals:
        if goal["status"]=="watching":
            alternate.journal.append("pause_goal_set",{
                "run_id":run_id,"goal_id":goal["id"],"question":goal["question"],"armed_after":goal["armed_after"],
                "source_run":parent.run_id,"source_record":goal["record"],
            },spec.window)
    alternate.status, alternate.error = "paused", None
    alternate.journal.append("run_status",alternate.snapshot(),spec.window)
    watch_file=parent.journal.directory/"observer.json"
    if watch_file.is_file():
        # The complete sidecar is replaced atomically by the UI. Each route keeps its own copy.
        (directory/"observer.json").write_bytes(watch_file.read_bytes())
    return alternate


def experiment_comparison(parent, alternate, window=None):
    marker = alternate.experiment
    if not marker or marker["parent_id"] != parent.run_id:
        raise ValueError("两条路线没有共同的分岔记录")
    if marker["kind"] in {"extend","model_continue"}:
        start=marker["window"]
        latest=alternate.window
        end=latest if window is None else window
        if not start<=end<=latest:
            raise ValueError("请选择延长起点至最新已结算窗口")
        result=compare(parent,alternate,start,end)
        changed=[w for w in range(start+1,end+1) if alternate.journal.settlement(w)["data"]["changes"]]
        return {**result,"mode":"continuation","experiment":marker,"latest":latest,"observed":end-start,
                "changed_windows":len(changed),"first_change":changed[0] if changed else None}
    result=trajectory_comparison(parent,alternate,marker["window"],window)
    if marker["kind"]=="delivery_time":
        dispatch=marker["delivery_move"]["dispatch_id"]
        selected=result["right"]["window"]
        deliveries=[]
        for sim in (parent,alternate):
            sent=sim.journal.event(dispatch)
            arrived=sim.journal.arrival(dispatch,selected,sim._discarded)
            deliveries.append({"due":sim.mail_timing.due(sent,selected),
                               "arrival":arrived["window"] if arrived else None,"receipt":arrived["id"] if arrived else None})
        result["delivery_comparison"]={"dispatch":dispatch,"sides":deliveries}
    return {**result,"experiment":marker}


def trajectory_comparison(parent,alternate,split,window=None):
    latest = min(parent.window, alternate.window)
    selected = latest if window is None else window
    if not split <= selected <= latest:
        raise ValueError("只能对照两条路线都已提交的窗口")
    result = compare(parent, alternate, selected, selected)
    first_difference = 0 if split==0 and parent._scenario.initial_state()!=alternate._scenario.initial_state() else None
    first_event_difference=None
    has_calendar=bool(getattr(parent._scenario,"scheduled_events",()) or getattr(alternate._scenario,"scheduled_events",()))
    def actual_events(sim,w):
        rows,_=sim.journal.window_rows(w,0,kind="external_event")
        return [e for e in rows if e["id"] not in sim._discarded]
    def signatures(sim,w):
        keys=set()
        for e in actual_events(sim,w):
            d=e["data"]
            keys.add(encode([d["id"],d["text"],d["visibility"]["public"],sorted(d["visibility"]["readers"]),d["assignments"]]))
        return keys
    for number in range(split + 1, latest + 1):
        baseline = parent.journal.settlement(number)["data"]
        variant = alternate.journal.settlement(number)["data"]
        if first_difference is None and (baseline["changes"] or variant["changes"]) and baseline["state"] != variant["state"]:
            first_difference = number
        if has_calendar and first_event_difference is None and signatures(parent,number)!=signatures(alternate,number):
            first_event_difference=number
        if first_difference is not None and (not has_calendar or first_event_difference is not None):
            break
    def evidence(sim):
        commit = sim.journal.settlement(selected)
        if not commit:
            return {"consequences":[],"actions":[],"events":[]}
        rows, _ = sim.journal.window_rows(selected,0)
        actions = [{"id":e["id"],"actor_id":e["data"]["actor_id"],"round":e["data"]["round"],"kind":e["kind"],
                    "text":e["data"].get("text") or "延续此前的等待安排，本轮没有新的表达。"}
                   for e in rows if e["kind"] in {"expression","decision_deferred"} and sim.event_status(e)=="committed"]
        events=[{"record":e["id"],"text":e["data"]["text"],"visibility":e["data"]["visibility"]} for e in actual_events(sim,selected)]
        return {"consequences":commit["data"]["consequences"],"actions":actions,"events":events}
    result.update(latest=latest, first_state_difference=first_difference,first_event_difference=first_event_difference,
                  baseline=evidence(parent), variant=evidence(alternate))
    return result


def outcome_groups(parent, catalog, actor_id=None, field_id=None):
    fields = [{"actor":a.id, "field":f.id, "label":a.name+" · "+(f.label or f.id), "unit":f.unit}
              for a in parent.scenario.entities for f in a.fields]
    selected = next((f for f in fields if f["actor"]==actor_id and f["field"]==field_id),None) if actor_id or field_id else next(iter(fields),None)
    if (actor_id or field_id) and not selected:
        raise ValueError("所选字段不属于原路线")
    groups = {}
    for row in catalog:
        spec = row.get("experiment")
        if not spec or spec["parent_id"] != parent.run_id:
            continue
        key = (spec["parent_event"],spec["state_sha256"],spec["through"],spec["kind"],spec["condition"],spec["audience"],tuple(sorted(spec["readers"])),encode(spec.get("assignment")),encode(spec.get("route")),encode(spec.get("event_move")),spec.get("ends_window"),encode(spec.get("model_change")))
        if key not in groups:
            original = boundary(parent,spec["window"] if spec["kind"] in {"extend","model_continue"} else spec["through"])
            groups[key] = {"spec":spec,"baseline":original["state"][selected["actor"]][selected["field"]] if selected else None,
                           "completed":0,"outcomes":{},"runs":[]}
        group = groups[key]
        group["runs"].append({k:row[k] for k in ("run_id","status","window")})
        if row["status"]=="completed" and row["window"]==spec["through"] and row.get("end_state") is not None:
            group["completed"] += 1
            if selected:
                value = row["end_state"][selected["actor"]][selected["field"]]
                entry = group["outcomes"].setdefault(encode(value),{"value":value,"count":0})
                entry["count"] += 1
    return {"fields":fields,"selected":selected,"actors":{a.id:a.name for a in parent._scenario.entities},
            "groups":[{**g,"outcomes":list(g["outcomes"].values())} for g in groups.values()]}
