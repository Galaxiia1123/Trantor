"""Turn a new observer question into an editable, admissible experiment on an existing route."""
import re

from pydantic import Field, ValidationError

from .continuity import terms
from .engine import encode
from .experiments import prepare_experiment
from .ideas import ComparisonIdea, explicit_ideas
from .models import protect_credentials
from .schema import Record, Source
from .studies import StudyRequest


class PlanQuestion(Record):
    question: str = Field(min_length=1,max_length=3000)
    default_split: int = Field(ge=0,strict=True)
    allow_model: bool = True


class PlanReply(Record):
    clarification: str = Field(default="",max_length=400)
    ideas: list[ComparisonIdea] = Field(default_factory=list,max_length=2)


PLANNER = """你协助观察者把一个新的“如果”问题变成可编辑对照。只使用目录中的主体、字段、线路和事件。
一次改变一个条件。数值实验用 field 和 assignment；同一数值字段的其他档位用 additional_values。具体事件改期用 event_time；
通信时效用 transit。自由条件用 condition 并明确起初的知情范围。不要替主体预先决定同意、拒绝或结果。
window 是保留到的最后一个已结算窗口，through 是观察终点，必须在给出的范围内。没有明确时点时用 default_split。
操作者已经选好了起点。文中的“下一窗”或与 window_duration 对应的“下一班次”等相对时间，就是 default_split 后的新窗口，并非需要目录提供的外部事件。能据此拟定时直接给方案。
按用户明确指定的数值、时点、方向和告知对象安排。资料不足以确定重要差别时用 clarification 集中提一个具体问题。
可以附一个已定义字段的 watch，不能发明数据。只拟定方案，不运行模拟。不要把问题中的假设当成历史事实。返回 schema JSON。"""


def _single_value(sim,q,split,end):
    named=[]
    for a in sim._scenario.entities:
        for f in a.fields:
            if f.kind=="number" and (f.label or f.id) in q:
                named.append((a,f))
    owners=[(a,f) for a,f in named if a.name in q or re.search(r"(?<![A-Za-z0-9_.-])"+re.escape(a.id)+r"(?![A-Za-z0-9_.-])",q)]
    if owners:named=owners
    match=re.search(r"(?:设为|设成|降到|降至|增至|提高到|增加到|减少到|调整为|改为|改成)\s*([-+]?\d+(?:\.\d+)?)",q)
    if not named or not match:return [],""
    if len(named)!=1:
        return [],"同名或相关的数值字段不止一个。请写明要改谁的哪项字段："+"、".join(a.name+"的"+(f.label or f.id) for a,f in named[:6])+"。"
    actor,field=named[0]
    unit=field.unit or "";tail=q[match.end():].lstrip()
    if tail and not (unit and tail.startswith(unit)) and not re.match(r"[，。？?、,；;！!]|(?:会|能|时|后|呢|来|并|是否|再|比较|看看)",tail):
        return [],"请确认目标值的单位"+("，这个字段使用“"+unit+"”" if unit else "")+"。"
    mark=re.search(r"第\s*(\d+)\s*窗(?:口)?(?:末|结束时)",q[:match.start()])
    at=int(mark[1])-1 if mark else split
    stop=re.search(r"(?:比较|观察|到|至)[^。！？]{0,12}?第\s*(\d+)\s*窗",tail)
    num=float(match[1]) if "." in match[1] else int(match[1])
    watch=[(a,f) for a in sim._scenario.entities for f in a.fields if (a.id,f.id)!=(actor.id,field.id) and (f.label or f.id) in tail]
    return [{"question":q[:160],"reason":"问题中明确给出了字段和目标值，可在启动前调整。",
             "experiment":{"window":at,"through":int(stop[1]) if stop else end,"kind":"field",
                           "assignment":{"entity_id":actor.id,"field_id":field.id,"value":num}},
             "watch":{"actor_id":watch[0][0].id,"field_id":watch[0][1].id} if len(watch)==1 else None}],""


def _requested_audience(question,actors):
    if re.search(r"仅世界(?:系统)?知情|只(?:让|有)世界(?:系统)?知道|不(?:提前)?通知任何主体",question):
        return ("world",[]),""
    if re.search(r"向所有主体公开|通知(?:全部|所有)主体|公开(?:通知|告知)各方",question):
        return ("public",[]),""
    clause=re.search(r"(?:只|仅)(?:提前)?(?:通知|告诉|告知|让)([^，。；？！,;!?\n]{1,100})",question)
    if not clause:return None,""
    part=re.split(r"知道|知晓|知情|获知|得知|收到",clause[1])[0].strip()
    readers=[]
    for item in re.split(r"、|及|和|与",part):
        item=item.strip(" “「」”")
        exact=[a for a in actors if item in (a.name,a.id)]
        matches=exact or [a for a in actors if len(item)>=2 and a.name.endswith(item)]
        if len(matches)!=1:return None,"请写出所有最初应收到通知的主体名称，避免把这项条件告诉其他人。"
        readers.append(matches[0].id)
    return ("readers",list(dict.fromkeys(readers))),""


def _catalog(sim,question,budget,split):
    rows=[]
    commit=sim.journal.settlement(split)
    state=commit["data"]["state"] if commit else sim._scenario.initial_state()
    for a in sim._scenario.entities:
        rows.append({"actor":a.id,"name":a.name,"role":a.identity[:150]})
        for f in a.fields:
            rules=f.constraints
            rows.append({"actor":a.id,"name":a.name,"field":f.id,"label":f.label or f.id,"type":f.kind,"unit":f.unit,
                         "value_at_default_split":state[a.id][f.id],"editable":not (rules and rules.fixed),
                         **({"minimum":rules.minimum,"maximum":rules.maximum} if rules and f.kind=="number" else {}),
                         **({"choices":rules.choices[:40],"total_choices":len(rules.choices)} if rules and rules.choices else {})})
    for edge in sim._scenario.influences:
        rows.append({"relation":[edge.source,edge.target],"description":edge.description[:180]})
    for e in sim._scenario.scheduled_events:
        rows.append({"event":e.id,"window":e.window,"text":e.text[:240]})
    for r in sim._scenario.mail_routes:
        rows.append({"from":r.source,"to":r.target,"active_from":r.active_from,"transit_windows":r.transit_windows})
    query=terms(question);ordered=sorted(rows,key=lambda obj:len(terms(encode(obj)) & query),reverse=True)
    kept=[]
    for row in ordered:
        size=len(encode(row))+2
        if size<=budget:kept.append(row);budget-=size
    return kept,len(rows)


def _previous_reply(sim,config_id,messages,window):
    rows=sim.journal.selected({"analysis_input","analysis_output"},transport_details=False)
    candidates={e["id"] for e in rows if e["kind"]=="analysis_input" and e["window"]==window
                and e["data"].get("purpose")=="analysis_plan" and e["data"].get("config_id")==config_id}
    for e in reversed(rows):
        if e["kind"]!="analysis_output" or e["data"].get("input_id") not in candidates:continue
        previous=sim.journal.event(e["data"]["input_id"])
        if previous["data"]["messages"]!=messages:continue
        try:return PlanReply.model_validate_json(sim.journal.event(e["id"])["data"]["text"]),e["id"]
        except ValidationError:continue
    return None,None


def plan_question(sim,request,progress=None,*,job_id=None,before_model=None):
    q=request.question.strip()
    if not q:raise ValueError("请写下想在这条路线中尝试的问题")
    protect_credentials(q,sim.config.catalog.values())
    with sim._lock:
        earliest=(sim.experiment or {}).get("window",0);end=sim.window
    if not hasattr(sim.config,"model") or not earliest<=request.default_split<end:
        raise ValueError("请先选择一个已结算的对照时段")
    source=Source(id="observer_question",name="观察者刚提出的问题",text=q)
    raw=[];origin="observer";calls=0;output_id=None
    audience,clarification=_requested_audience(q,sim._scenario.entities)
    changes=re.findall(r"设为|设成|降到|降至|提高到|增加到|减少到|调整为|改为|改成",q)
    if len(changes)>1:clarification="一条对照路线会保留其他条件。你想先比较其中哪一项改动？"
    # A request about who knows the intervention must go through the audience-aware planner.
    if not clarification and (audience or not re.search(r"通知|告诉|告知|仅.{0,15}知道|只让|公开宣布|保密|隐瞒",q)):
        raw=list(explicit_ideas(sim._scenario,sim.config,earliest,[source],request.default_split))[:2]
        if not raw:raw,clarification=_single_value(sim,q,request.default_split,end)
    model=sim.config.model;method="direct"
    if not raw and not clarification and request.allow_model and model.provider!="demo":
        model_id=sim.config.model_id("report")
        prompt={"task":"experiment_plan","question":q,"scene":sim._scenario.title,"earliest_split":earliest,"default_split":request.default_split,
                "next_window":request.default_split+1,"window_duration":sim.config.window_duration,"rounds_per_window":sim.config.rounds_per_window,
                "latest_committed":end,"schema":PlanReply.model_json_schema()}
        budget=min(18000,model.context_limit_chars-len(PLANNER)-len(encode(prompt))-200)
        if budget<250:raise ValueError("模型输入限额放不下实验目录，请提高限额或改用已有的手动对照表单")
        items,total=_catalog(sim,q,budget,request.default_split);prompt.update(catalog=items,available=total)
        if progress:progress("planning","按这条路线的场景与已结算时段拟定方案")
        messages=[{"role":"system","content":PLANNER},{"role":"user","content":encode(prompt)}]
        parsed,output_id=_previous_reply(sim,model_id,messages,end)
        if parsed is None:
            if before_model:before_model()
            entry=sim.journal.append("analysis_input",{"purpose":"analysis_plan","config_id":model_id,"messages":messages},end)
            reply=sim.call_recorded(model_id,messages,entry)
            output_id=sim.journal.append("analysis_output",{"input_id":entry["id"],"text":reply},end)["id"]
            try:parsed=PlanReply.model_validate_json(reply)
            except ValidationError:raise ValueError("模型给出的方案格式无法读取，本次未启动路线") from None
            calls=1
        raw=parsed.ideas;clarification=parsed.clarification;origin="model";method="model" if calls else "reused"
    ideas=[];issues=[]
    for proposal in raw:
        try:
            idea=ComparisonIdea.model_validate(proposal);spec=idea.experiment
            if audience:
                if spec.kind not in {"field","condition"}:raise ValueError("这项改动会沿用既有通知范围，请先选定要改变的是事件或线路，还是最初的知情对象")
                spec=spec.model_copy(update={"audience":audience[0],"readers":audience[1]})
                idea=idea.model_copy(update={"experiment":spec})
            if spec.kind in {"repeat","extend"}:raise ValueError("原样重复或延长可直接从观察台启动")
            if spec.through>end or spec.window<earliest:raise ValueError("拟定的时段超出了原路线可对照的范围")
            if idea.additional_values:
                study=StudyRequest(experiment=spec,additional_values=idea.additional_values,repetitions=1,call_limit=1)
                for alt in study.conditions().values():prepare_experiment(sim,alt,allow_same_field=True)
            _,_,condition=prepare_experiment(sim,spec,allow_same_field=bool(idea.additional_values))
            watch_note=""
            if idea.watch:
                watch=idea.watch
                if watch.operator=="track" and watch.target is None and watch.deadline in (None,spec.through) and watch.criterion in {"ever","at_end"}:
                    # A plain final value already uses the experiment's common endpoint.
                    watch=watch.model_copy(update={"deadline":None,"criterion":"ever"})
                try:watch.resolve(sim._scenario,sim.config.catalog.values())
                except ValueError as error:
                    watch_note="这项观察卡需要调整："+str(error)+"。可在实验设置中另选关注字段。"
                    watch=None
                idea=idea.model_copy(update={"watch":watch})
            data=idea.model_dump(mode="json")
            data.update(origin=origin,condition=condition,ready=True,quote={"source_id":source.id,"quote":q[:1200]})
            if watch_note:data["watch_note"]=watch_note
            protect_credentials(data,sim.config.catalog.values());ideas.append(data)
        except (ValidationError,ValueError) as exc:
            if isinstance(exc,ValidationError):issues.append("方案没有对应到可用的场景字段或事件")
            else:issues.append(str(exc))
    if not ideas and not clarification:
        clarification="；".join(dict.fromkeys(issues)) or "请写明要改变哪个主体的字段、哪条通信线路或哪个日历事件，并给出想比较的取值或时间。"
    result={"question":q,"ideas":ideas,"clarification":clarification,"method":method,"calls":calls,
            "available_through":end,"default_split":request.default_split,"job_id":job_id,"output_id":output_id}
    record=sim.journal.append("analysis_plan",result,end)
    return {**result,"record_id":record["id"]}
