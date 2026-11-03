"""Serial repetitions of a sealed experiment, with one shared decision/settlement budget."""
from __future__ import annotations

import json
import re
import threading
import uuid
from decimal import Decimal, localcontext
from pathlib import Path

from pydantic import Field, StrictFloat, StrictInt, ValidationError, model_validator

from .engine import encode
from .experiments import ExperimentRequest, prepare_experiment, trajectory_comparison
from .models import protect_credentials
from .observatory import FieldWatch, note_text, observe_field
from .repository import VersionConflict, atomic_json, now
from .schema import Record


class StudyRequest(Record):
    experiment: ExperimentRequest
    repetitions: int = Field(default=3, ge=1, le=16)
    compare_original: bool = False
    additional_values: list[StrictInt | StrictFloat] = Field(default_factory=list,max_length=7)
    call_limit: int = Field(gt=0, le=1_000_000)
    watch: FieldWatch | None = None

    @model_validator(mode="after")
    def distinct_conditions(self):
        if self.experiment.kind=="extend":
            raise ValueError("延长观察请从已结束的单条路线启动")
        if self.additional_values:
            first=self.experiment.assignment
            if self.experiment.kind not in {"field","opening"} or first is None or type(first.value) not in (int,float):
                raise ValueError("多档实验需要选择同一个数值字段和第一个目标值")
            if self.experiment.condition.strip():
                raise ValueError("多档实验沿用相同的通知方式，请在字段中设定值并留空自定义条件说明")
            values=[first.value,*self.additional_values]
            if any(not Decimal(str(v)).is_finite() for v in values) or len(set(values))!=len(values):
                raise ValueError("各档必须是不重复的有限数值")
            if (len(values)+int(self.compare_original))*self.repetitions>32:
                raise ValueError("一组多档实验最多安排 32 条路线，请减少取值或重复次数")
        if self.compare_original and self.experiment.kind=="repeat":
            raise ValueError("双条件对照需要具体改变一个条件；原样重跑可直接使用重复实验")
        if not self.compare_original and not self.additional_values and self.repetitions<2:
            raise ValueError("重复实验至少需要两条路线")
        return self

    def conditions(self):
        result={}
        if self.compare_original:
            result["original"]=ExperimentRequest(window=self.experiment.window,through=self.experiment.through,kind="repeat")
        if self.additional_values:
            assignment=self.experiment.assignment
            for i,value in enumerate([assignment.value,*self.additional_values],1):
                result[f"level_{i}"]=self.experiment.model_copy(update={"assignment":assignment.model_copy(update={"value":value})})
        else:
            result["changed" if self.compare_original else None]=self.experiment
        return result


def measured(sim, watch, through):
    if watch is None:
        return None
    result = observe_field(sim, watch, through, details=False, close_horizon=True)
    return {k:result[k] for k in ("actor","label","unit","kind","value","numeric_text","record","status","first_hit")}


def outcomes_for(members):
    groups={}
    samples=[]
    completed=unknown=0
    for member in members:
        if member["status"]!="completed":
            continue
        completed+=1
        result=member["outcome"]
        if result is None:
            continue
        if result["status"]!="tracking":
            key=("finding",result["status"])
        elif result["numeric_text"] is not None:
            number=Decimal(result["numeric_text"])
            literal=format(number,"f") if number else "0"
            if "." in literal:
                literal=literal.rstrip("0").rstrip(".")
            key=("number",literal)
            samples.append(number)
        else:
            key=("value",encode(result["value"]))
            unknown+=result["value"] is None
        group=groups.setdefault(key,{"key":encode(key),"example":result,"indices":[]})
        group["indices"].append(member["index"])
    numeric=None
    if samples:
        samples.sort()
        middle=len(samples)//2
        with localcontext() as ctx:
            ctx.prec=max(28,max(n.adjusted() for n in samples)-min(n.as_tuple().exponent for n in samples)+3)
            median=samples[middle] if len(samples)%2 else (samples[middle-1]+samples[middle])/2
        numeric={"known":len(samples),"unknown":unknown,"min":str(samples[0]),"max":str(samples[-1]),"median":str(median)}
    return {"total":len(members),"completed":completed,"calls":sum(m["calls"] for m in members),
            "outcomes":list(groups.values()),"numeric":numeric}


def member_marker(record,member):
    marker={"id":record["id"],"index":member["index"]}
    if "arm" in member:
        marker.update(arm=member["arm"],replicate=member["replicate"])
    return marker


def study_notes(record):
    spec=record["request"]["experiment"]
    labels={"original":"原条件","changed":"改后条件","single":"新路线","queued":"未启动","opening":"正在恢复",
            "completed":"已完成","stopped":"已结束","running":"运行中","failed":"失败","paused":"已暂停","interrupted":"中断",
            "limit":"已到上限","pausing":"正在暂停","stopping":"正在结束"}
    findings={"observed":"出现过目标结果","not_observed":"未出现目标结果","maintained":"全程满足","violated":"曾偏离条件",
              "met_at_end":"终点达成","missed_at_end":"终点未达成","undetermined":"有未知窗口","undetermined_end":"终值未知","ended_early":"未覆盖期限"}
    labels.update({a["arm"]:note_text(a["label"]) for a in record.get("arm_definitions",())})
    def outcome(row):
        if not row:return "—"
        if row["status"]!="tracking":return findings.get(row["status"],row["status"])
        value=row["value"]
        if value is None:return "未知"
        if isinstance(value,bool):return "是" if value else "否"
        return note_text(row["numeric_text"] or value)+note_text(row.get("unit") or "")
    lines=["# "+note_text(record["title"])+" · 条件实验","",
           f'实验组 `{record["id"]}`，原路线 `{record["parent_id"]}`。从第 {spec["window"]} 窗后运行到第 {spec["through"]} 窗。',
           "",note_text(record["condition"] or "保留原条件重复运行。"),"",
           f'已完成 {record["completed"]}/{len(record["members"])} 条。全组主体与世界模型调用 {record["calls"]}/{record["request"]["call_limit"]}。']
    b=record["baseline"]
    if b:
        title=(record["request"]["watch"] or {}).get("title") or b["actor"]+" · "+b["label"]
        lines+=["","## "+note_text(title),"",f'存档原路线的结果为{outcome(b)}，依据 `{b["record"]}`。']
    for arm in record.get("arms") or [{"arm":"single",**outcomes_for(record["members"])}]:
        label=labels[arm["arm"]]
        lines+=["","### "+label,"",f'{arm["completed"]}/{arm["total"]} 条到达共同终点。']
        for o in arm["outcomes"]:
            indices="、".join(str(i) for i in o["indices"])
            lines.append(f'- {outcome(o["example"])}，{len(o["indices"])}/{arm["completed"]} 次，对应路线 {indices}。')
        if (stats:=arm["numeric"]) and stats["known"]>=2:
            unit=note_text((b or {}).get("unit") or "")
            lines.append(f'已知数值 {stats["known"]} 个，中位数 {stats["median"]}{unit}，范围 {stats["min"]}{unit} 至 {stats["max"]}{unit}。')
    lines+=["","## 路线与依据","","| 路线 | 条件 | 状态 | 提交窗口 | 调用 | 观测结果 | 运行与证据编号 |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for m in record["members"]:
        doc=m["outcome"]
        evidence=f'`{m["run_id"]}`'+(f' / `{doc["record"]}`' if doc else "") if m.get("created") or m["status"]=="completed" else "—"
        lines.append(f'| {m["index"]} | {labels[m.get("arm","single")]} | {labels.get(m["status"],m["status"])} | {m["window"]} | {m["calls"]} | {outcome(doc)} | {evidence} |')
    lines+=["","次数来自本机已完成的独立推演。未完成路线保留在清单中。",""]
    return "\n".join(lines)


class Studies:
    def __init__(self, root, get_run, fork, check_parent):
        self.root = Path(root)
        self.get_run = get_run
        self.fork = fork
        self.check_parent = check_parent
        self.lock = threading.RLock()
        self.workers = {}
        self.live = {}

    def path(self, study_id):
        if not re.fullmatch(r"[a-f0-9]{32}", study_id):
            raise ValueError("实验组编号无效")
        return self.root / "_studies" / (study_id + ".json")

    def read(self, study_id):
        return json.loads(self.path(study_id).read_text("utf-8"))

    def save(self, record, *, new=False):
        record["updated_at"] = now()
        atomic_json(self.path(record["id"]), record, create_only=new)

    def create(self, study_id, parent, request):
        payload = request.model_dump(mode="json")
        with self.lock:
            if self.path(study_id).exists():
                existing = self.read(study_id)
                prior=StudyRequest.model_validate(existing["request"]).model_dump(mode="json")
                if existing["parent_id"] != parent.run_id or prior != payload:
                    raise VersionConflict("这个实验组编号已经用于其他条件")
                return self.view(study_id)
            self.check_parent(parent)
            protect_credentials(payload, parent.config.catalog.values())
            options=request.conditions()
            if request.additional_values:
                # Every level must be admissible before the first independent run can spend a call.
                for exp in options.values():
                    prepare_experiment(parent,exp,allow_same_field=True)
                a=request.experiment.assignment
                actor=next(e for e in parent._scenario.entities if e.id==a.entity_id)
                field=next(f for f in actor.fields if f.id==a.field_id)
                labels=[{"arm":key,"label":str(exp.assignment.value)+(field.unit or ""),"value":exp.assignment.value} if exp.assignment
                        else {"arm":key,"label":"原条件"} for key,exp in options.items()]
                condition=actor.name+" · "+(field.label or field.id)+"的多档对照"
            else:
                labels=[]
                _, _, condition = prepare_experiment(parent, request.experiment)
            baseline = measured(parent, request.watch, request.experiment.through)
            members = []
            for replicate in range(1,request.repetitions+1):
                arms=list(options)
                if request.additional_values:
                    offset=(replicate-1)%len(arms)
                    arms=arms[offset:]+arms[:offset]
                elif replicate%2==0:
                    arms.reverse()
                for arm in arms:
                    index=len(members)+1
                    key=uuid.uuid5(uuid.NAMESPACE_URL,f"trantor:study:{study_id}:{index}").hex
                    member={"index":index,"run_id":key[:12],"status":"queued","calls":0,"window":request.experiment.window,"outcome":None}
                    if arm:
                        member.update(arm=arm,replicate=replicate)
                    members.append(member)
            record = {"id":study_id, "parent_id":parent.run_id, "title":parent._scenario.title,
                      "condition":condition, "request":payload, "created_at":now(), "status":"running",
                      "baseline":baseline, "members":members, "error":None, **({"arm_definitions":labels} if labels else {})}
            self.save(record, new=True)
            self._start(study_id)
            return self.view(study_id)

    def _start(self, study_id):
        signal = threading.Event()
        worker = threading.Thread(target=self._execute, args=(study_id, signal), name="study-"+study_id[:8], daemon=True)
        self.workers[study_id] = (worker, signal)
        worker.start()

    @staticmethod
    def _check_member(sim, record, member):
        marker = sim.experiment or {}
        if marker.get("study") != member_marker(record,member) or marker.get("parent_id") != record["parent_id"]:
            raise ValueError("路线记录与实验组不匹配，请检查本机资料")
        if record.get("arm_definitions"):
            spec=StudyRequest.model_validate(record["request"]).conditions()[member["arm"]]
            expected=spec.model_dump(mode="json")
            if any(marker.get(k)!=expected[k] for k in ("kind","assignment","window","through","audience","readers")):
                raise ValueError("路线记录与本档的数值、时间或通知范围不符")

    def _capture(self, record, member, sim):
        self._check_member(sim, record, member)
        snapshot = sim.snapshot(known_window=sim.window)
        member.update(status=snapshot["status"], window=snapshot["window"], calls=snapshot["model_calls"])
        if member["status"] == "completed":
            spec = StudyRequest.model_validate(record["request"])
            if member["window"] != spec.experiment.through:
                raise ValueError("实验路线的终点与约定不一致")
            member["outcome"] = measured(sim, spec.watch, member["window"])

    def _refresh(self, record):
        """A child journal may have committed before the group receipt was replaced."""
        study_id = record["id"]
        member = next((m for m in record["members"] if m["status"] not in {"completed","queued"}), None)
        if member:
            sim = self.live.get(study_id)
            if sim is None and study_id not in self.workers and (self.root/member["run_id"]).is_dir():
                sim = self.get_run(member["run_id"])
            if sim:
                self._capture(record, member, sim)
        if all(m["status"]=="completed" for m in record["members"]):
            record.update(status="completed", error=None)
        elif study_id not in self.workers:
            prior = record["status"]
            if prior in {"running","pausing","stopping"}:
                record["status"] = {"running":"interrupted","pausing":"paused","stopping":"stopped"}[prior]
        return record

    def view(self, study_id):
        with self.lock:
            record = self._refresh(self.read(study_id))
            members=record["members"]
            if record.get("arm_definitions") or record["request"].get("compare_original"):
                definitions=record.get("arm_definitions") or [{"arm":"original"},{"arm":"changed"}]
                arms=[{**a,**outcomes_for([m for m in members if m["arm"]==a["arm"]])} for a in definitions]
                return {**record,"outcomes":[],"arms":arms,"calls":sum(g["calls"] for g in arms),"completed":sum(g["completed"] for g in arms)}
            return {**record,**outcomes_for(members),"arms":[]}

    def compare_members(self,study_id,original,changed,window):
        record=self.view(study_id)
        members={m["index"]:m for m in record["members"]}
        routes=[]
        multi=bool(record.get("arm_definitions"))
        if multi and members.get(original,{}).get("arm")==members.get(changed,{}).get("arm"):
            raise ValueError("请选取不同条件的两条路线")
        for index,arm in ((original,"original"),(changed,"changed")):
            item=members.get(index)
            if not item or (not multi and item.get("arm")!=arm) or item["status"]!="completed":
                raise ValueError("请从两个条件中分别选择一条已完成的路线")
            sim=self.get_run(item["run_id"])
            self._check_member(sim,record,item)
            routes.append(sim)
        left,right=routes
        if any(left.experiment[k]!=right.experiment[k] for k in ("parent_id","parent_event","state_sha256","window","through")):
            raise ValueError("所选路线的分岔起点或观察终点不一致")
        result=trajectory_comparison(left,right,left.experiment["window"],window)
        if watch:=record["request"].get("watch"):
            from .contrast import field_contrast
            result["focus"]=field_contrast(left,right,watch["actor_id"],watch["field_id"],result["right"]["window"])
        return {**result,"actors":[{"id":a.id,"name":a.name} for a in left._scenario.entities]}

    def catalog(self):
        with self.lock:
            rows = []
            for path in (self.root/"_studies").glob("*.json"):
                record = self.read(path.stem)
                if record["status"] in {"running","pausing","stopping"}:
                    self._refresh(record)
                rows.append({**{k:record[k] for k in ("id","parent_id","title","condition","status","updated_at")},
                             "count":len(record["members"]), "paired":record["request"].get("compare_original",False),
                             "levels":len(record.get("arm_definitions",())),
                             "completed":sum(m["status"]=="completed" for m in record["members"])})
            return sorted(rows, key=lambda r:r["updated_at"], reverse=True)

    def control(self, study_id, action):
        with self.lock:
            record = self._refresh(self.read(study_id))
            status = record["status"]
            if action == "resume":
                if status not in {"paused","interrupted","failed"} or study_id in self.workers:
                    raise VersionConflict("当前实验组不能继续")
                self.check_parent(self.get_run(record["parent_id"]))
                record.update(status="running", error=None)
                self.save(record)
                self._start(study_id)
            elif (action=="stop" and status not in {"completed","stopped","limit"}) or (action=="pause" and status=="running"):
                active = self.workers.get(study_id)
                record["status"] = ("stopping" if active else "stopped") if action=="stop" else ("pausing" if active else "paused")
                self.save(record)
                if active:
                    active[1].set()
            return self.view(study_id)

    def _execute(self, study_id, signal):
        try:
            while True:
                with self.lock:
                    record = self.read(study_id)
                    if signal.is_set():
                        record["status"] = "stopped" if record["status"]=="stopping" else "paused"
                        self.save(record)
                        return
                    member = next((m for m in record["members"] if m["status"]!="completed"), None)
                    if member is None:
                        record.update(status="completed", error=None)
                        self.save(record)
                        return
                    spec = StudyRequest.model_validate(record["request"])
                    prior_calls = sum(m["calls"] for m in record["members"] if m["status"]=="completed")
                    remaining = spec.call_limit - prior_calls
                    if remaining <= member["calls"]:
                        record.update(status="limit", error="实验组的调用上限已用完。")
                        self.save(record)
                        return
                    member["status"] = "opening"
                    self.save(record)
                parent = self.get_run(record["parent_id"])
                self.check_parent(parent)
                rid, index = member["run_id"], member["index"]
                if (self.root/rid).is_dir():
                    sim = self.get_run(rid)
                else:
                    if member.get("created"):
                        raise ValueError("已开始路线的本地记录缺失，无法继续计数。请先恢复原记录。")
                    exp=spec.conditions()[member.get("arm")]
                    sim=self.fork(parent,exp,rid,None,study=member_marker(record,member),call_limit=remaining,
                                  allow_same_field=bool(spec.additional_values))
                self._check_member(sim, record, member)
                with self.lock:
                    record = self.read(study_id)
                    record["members"][index-1]["created"] = True
                    self.save(record)
                    self.live[study_id] = sim
                if sim.status in {"interrupted","failed"} and sim.snapshot(known_window=sim.window)["can_resume"] and not signal.is_set():
                    sim.recover()
                if sim.status in {"ready","paused"}:
                    sim.run(pause_at_boundary=signal)
                with self.lock:
                    record = self.read(study_id)
                    member = record["members"][index-1]
                    self._capture(record, member, sim)
                    self.live.pop(study_id, None)
                    if all(m["status"]=="completed" for m in record["members"]):
                        record.update(status="completed", error=None)
                    elif sim.status=="failed":
                        exhausted = not sim.snapshot(known_window=sim.window)["can_resume"]
                        record.update(status="limit" if exhausted else "failed", error=sim.error)
                    elif signal.is_set() or sim.status in {"stopped","paused","interrupted"}:
                        record["status"] = "stopped" if record["status"]=="stopping" or sim.status=="stopped" else "paused"
                    self.save(record)
                    if record["status"]!="running":
                        return
        except Exception as exc:
            if isinstance(exc, ValidationError):
                message = "；".join(e["msg"] for e in exc.errors(include_input=False)[:4])
            else:
                message = str(exc) if isinstance(exc, ValueError) else f"{type(exc).__name__}，当前实验路线未完成"
            with self.lock:
                record = self.read(study_id)
                record.update(status="failed", error=message)
                self.save(record)
        finally:
            with self.lock:
                self.live.pop(study_id, None)
                self.workers.pop(study_id, None)

    def shutdown(self):
        with self.lock:
            active = list(self.workers.items())
            for study_id, (_, signal) in active:
                record = self.read(study_id)
                if record["status"]=="running":
                    record["status"]="pausing"
                    self.save(record)
                signal.set()
        for _, (worker, _) in active:
            worker.join()
