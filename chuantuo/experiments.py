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

from .models import protect_credentials
from .schema import Identifier, Provenance, Record, RunConfig11, Scenario11, Source, StateField, load_scenario


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
        revised = revised.model_copy(update={"rules":[rule.model_copy(update={"active_from":max(1,rule.active_from-window)}) for rule in revised.rules],
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


class ExperimentRequest(Record):
    window: int = Field(ge=0)
    through: int = Field(ge=1)
    kind: Literal["condition", "repeat"] = "condition"
    condition: str = Field(default="", max_length=1600)
    audience: Literal["public", "readers", "world"] = "world"
    readers: list[Identifier] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        if self.through <= self.window:
            raise ValueError("终点须晚于分岔窗口")
        if (self.kind == "condition") != bool(self.condition.strip()):
            raise ValueError("改变条件时请填写具体情境，原样重跑时请留空")
        if (self.audience == "readers") != bool(self.readers) or len(set(self.readers)) != len(self.readers):
            raise ValueError("请选择实际获知条件的主体")
        if self.kind == "repeat" and self.audience != "world":
            raise ValueError("原样重跑无需指定知情范围")
        return self


def fork_experiment(parent, spec, run_id, job_id, directory: Path):
    """Copy a sealed prefix once. Stable event positions preserve private inboxes and citations."""
    if not isinstance(parent.config, RunConfig11):
        raise ValueError("历史版本的场景可另存开局，条件对照需要 v1.1 场景")
    if spec.through > parent.window:
        raise ValueError("请选择原路线已经提交的终点")
    if parent.experiment and spec.window < parent.experiment["window"]:
        raise ValueError("请打开原路线以选择更早的分岔窗口")
    selected = boundary(parent, spec.window)
    scene = parent.scenario.model_dump(mode="json")
    condition = spec.condition.strip()
    if condition:
        scene["rules"].append({"text":condition, "active_from":spec.window + 1,
            "visibility":{"public":spec.audience == "public","readers":spec.readers},
            "provenance":{"kind":"user","explanation":f"对照实验设定，自第 {spec.window + 1} 窗起生效。"}})
    scene = Scenario11.model_validate(scene)
    protect_credentials(scene.model_dump(mode="json"), parent.config.catalog.values())
    config = parent.config.model_copy(update={"windows":spec.through})
    marker = {**spec.model_dump(mode="json"), "condition":condition, "parent_id":parent.run_id,
              "parent_event":selected["event_id"],
              "state_sha256":hashlib.sha256(encode(selected["state"]).encode()).hexdigest()}
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
            else:
                dest.write(raw)
                continue
            dest.write((encode(event) + "\n").encode())
    alternate = Simulation.restore(directory, gateway=parent.gateway)
    if alternate.window != spec.window or alternate.state != selected["state"]:
        raise ValueError("分岔状态与原路线提交不一致")
    alternate.journal.append("experiment_forked",marker,spec.window)
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
    split = marker["window"]
    latest = min(parent.window, alternate.window)
    selected = latest if window is None else window
    if not split <= selected <= latest:
        raise ValueError("只能对照两条路线都已提交的窗口")
    result = compare(parent, alternate, selected, selected)
    first_difference = None
    for number in range(split + 1, latest + 1):
        baseline = parent.journal.settlement(number)["data"]
        variant = alternate.journal.settlement(number)["data"]
        if (baseline["changes"] or variant["changes"]) and baseline["state"] != variant["state"]:
            first_difference = number
            break
    def evidence(sim):
        commit = sim.journal.settlement(selected)
        if not commit:
            return {"consequences":[],"actions":[]}
        rows, _ = sim.journal.window_rows(selected,0)
        actions = [{"id":e["id"],"actor_id":e["data"]["actor_id"],"round":e["data"]["round"],"kind":e["kind"],
                    "text":e["data"].get("text") or "延续此前的等待安排，本轮没有新的表达。"}
                   for e in rows if e["kind"] in {"expression","decision_deferred"} and sim.event_status(e)=="committed"]
        return {"consequences":commit["data"]["consequences"],"actions":actions}
    result.update(experiment=marker, latest=latest, first_state_difference=first_difference,
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
        key = (spec["parent_event"],spec["state_sha256"],spec["through"],spec["kind"],spec["condition"],spec["audience"],tuple(sorted(spec["readers"])))
        if key not in groups:
            original = boundary(parent,spec["through"])
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
