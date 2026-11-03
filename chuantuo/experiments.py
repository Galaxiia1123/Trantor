"""Prepare a new scenario at a committed boundary and compare unit-compatible field values."""
from __future__ import annotations

import hashlib
import math
from decimal import Decimal

from .engine import encode
from pydantic import Field

from .schema import Identifier, Provenance, Record, Source, StateField, load_scenario


def boundary(simulation, window=None):
    snap = simulation.snapshot()
    selected = snap["window"] if window is None else window
    if selected < 0 or selected > snap["window"]:
        raise ValueError("只能选择开局或已经提交的窗口")
    if selected == 0:
        return {"window":0,"event_id":"e000001","state":simulation.scenario.initial_state()}
    commit = next(e for e in simulation.journal.snapshot({"window_committed"}) if e["window"] == selected)
    return {"window":selected,"event_id":commit["id"],"state":commit["data"]["state"]}


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
            return {"actor_name":actor, "domain":field.domain,"kind":field.kind,"unit":field.unit,
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
