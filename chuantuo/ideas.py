"""Turn a research question into an editable alternative, separate from the simulated world."""
import re
from itertools import chain
from pydantic import Field, StrictFloat, StrictInt

from .experiments import ExperimentRequest, apply_intervention
from .models import protect_credentials
from .observatory import FieldWatch
from .schema import Record, SourceQuote
from .continuity import terms


class ComparisonIdea(Record):
    question: str = Field(min_length=1,max_length=160)
    reason: str = Field(default="",max_length=350)
    experiment: ExperimentRequest
    watch: FieldWatch | None = None
    quote: SourceQuote | None = None
    additional_values: list[StrictInt | StrictFloat] = Field(default_factory=list,max_length=7)


def _grounded_quote(quote,scene):
    source=next((s for s in scene.sources if s.id==quote.source_id),None) if quote else None
    if not source:
        return False
    if quote.page is None:
        return quote.quote in source.text
    return any(p.number==quote.page and quote.quote in source.text[p.text_start:p.text_end] for p in source.pages)


def _explicit_date_question(scene,config,earliest,sources):
    move=re.compile(r"(?:从|由|原定)(?:在)?\s*第?\s*(\d{1,6})\s*窗(?:口)?\s*(?:推迟|延后|延迟|提前|挪|移|调整|改)(?:到|至|为)\s*第?\s*(\d{1,6})\s*窗")
    ordinary={"如果","同一","一次","原定","计划","安排","事件","日期","发生","窗口","提前","推迟","这个","这项","时候"}
    for source in sources:
        if source.public_reference or source.simulation_ref:
            continue
        for match in re.finditer(r"(?:如果|假如|若)[^。？！!?\n]{1,500}[？?]?",source.text):
            question=match.group()
            shift=move.search(question)
            if not shift:continue
            old,new=map(int,shift.groups())
            candidates=[e for e in scene.scheduled_events if e.window==old and earliest<min(old,new)]
            anchor=terms(question[:shift.start()])-ordinary
            matched=[e for e in candidates if e.id in question or len(anchor.intersection(terms(e.text)))>=2]
            if len(matched)!=1:continue
            end=re.search(r"第\s*(\d{1,6})\s*窗(?:末|结束时)",question[shift.end():])
            through=int(end[1]) if end else min(config.windows,max(old,new))
            fields=[(a,f) for a in scene.entities for f in a.fields if (f.label or f.id) in question]
            watch={"actor_id":fields[0][0].id,"field_id":fields[0][1].id} if len(fields)==1 else None
            yield {"question":question[:160],"reason":"材料明确给出了两个日期，其他事件设置沿用原路线。","quote":{"source_id":source.id,"quote":question},
                   "experiment":{"kind":"event_time","window":max(earliest,min(old,new)-1),"through":through,
                                 "event_move":{"event_id":matched[0].id,"new_window":new}},"watch":watch}


def _explicit_levels(scene,config,earliest,sources,default_split):
    fields=[(a,f) for a in scene.entities for f in a.fields]
    number=r"[-+]?\d+(?:\.\d+)?"
    for source in sources:
        if source.public_reference or source.simulation_ref:continue
        for sentence in re.findall(r"[^。！？!?\n]{1,450}[。！？!?]?",source.text):
            mark=re.search(r"(?:分别|逐档)(?:设为|设置为|设成|取|测试|试试|试)\s*",sentence)
            if not mark:continue
            choices=[(a,f) for a,f in fields if f.kind=="number" and (f.label or f.id) in sentence]
            if len(choices)>1:choices=[(a,f) for a,f in choices if a.name in sentence]
            if len(choices)!=1:continue
            actor,field=choices[0]
            unit=r"(?:\s*"+re.escape(field.unit)+r")?" if field.unit else ""
            token=number+unit
            values=re.match(token+r"(?:\s*(?:、|，|,|和|与|或)\s*"+token+r"){1,7}",sentence[mark.end():])
            if not values or re.search(r"\d,\d{3}(?:\D|$)",values.group()):continue
            rest=sentence[mark.end()+values.end():]
            if re.match(r"\s*(?:、|，|,|和|与|或)\s*[-+]?\d",rest):continue
            numbers=[float(v) if "." in v else int(v) for v in re.findall(number,values.group())]
            start=re.search(r"第\s*(\d+)\s*窗(?:口)?(?:末|结束时)",sentence[:mark.start()])
            stop=re.search(r"(?:比较|观察|到|至)[^。！？]{0,12}?第\s*(\d+)\s*窗",rest)
            split=int(start[1])-1 if start else default_split
            if split<earliest:continue
            watch=[(a,f) for a,f in fields if (a.id,f.id)!=(actor.id,field.id) and (f.label or f.id) in rest]
            yield {"question":sentence.strip()[:160],"reason":"文稿列出了同一字段的多个取值。每档在同一窗末设置，默认只在结算后通过字段的可见范围体现。可编辑通知对象和终点。",
                   "quote":{"source_id":source.id,"quote":sentence.strip()},
                   "experiment":{"kind":"field","window":split,"through":int(stop[1]) if stop else config.windows,
                                 "assignment":{"entity_id":actor.id,"field_id":field.id,"value":numbers[0]}},
                   "additional_values":numbers[1:],
                   "watch":{"actor_id":watch[0][0].id,"field_id":watch[0][1].id,"title":watch[0][1].label or watch[0][1].id} if len(watch)==1 else None}


def explicit_ideas(scene,config,earliest,sources,default_split):
    return chain(_explicit_date_question(scene,config,earliest,sources),_explicit_levels(scene,config,earliest,sources,default_split))


def useful_ideas(scene,config,proposals=(),watches=(),*,earliest=0,reviewed=False):
    ideas=[]
    def add(item,origin):
        try:
            idea=ComparisonIdea.model_validate(item)
            spec=idea.experiment
            if spec.window<earliest or spec.through>config.windows or spec.kind in {"repeat","extend"}:
                return
            protect_credentials(idea.model_dump(mode="json"),config.catalog.values())
            if idea.watch:
                try:
                    idea.watch.resolve(scene,config.catalog.values())
                except ValueError:
                    idea=idea.model_copy(update={"watch":None})
            state=scene.initial_state() if spec.window==0 else None
            if idea.additional_values:
                from .studies import StudyRequest
                study=StudyRequest(experiment=spec,additional_values=idea.additional_values,repetitions=1,call_limit=1)
                conditions=[apply_intervention(scene,alternative,"proposed",state,config.catalog.values(),allow_same_field=True)[1]
                            for alternative in study.conditions().values()]
                condition="共 "+str(len(conditions))+" 档，首档："+conditions[0]
            else:
                _,condition=apply_intervention(scene,spec,"proposed",state,config.catalog.values())
            grounded=_grounded_quote(idea.quote,scene)
        except ValueError:
            return
        snapshot=spec.model_dump(mode="json")
        if origin=="model" and idea.additional_values:
            values={spec.assignment.value,*idea.additional_values}
            for prior in ideas:
                other=prior["experiment"].get("assignment")
                if prior["origin"]=="material" and other and prior.get("additional_values") and (
                        other["entity_id"],other["field_id"])==(spec.assignment.entity_id,spec.assignment.field_id) and (
                        {other["value"],*prior["additional_values"]}==values):
                    return
        if any(row["experiment"]==snapshot or (spec.event_move and row["experiment"].get("event_move")==snapshot["event_move"]) for row in ideas):
            return
        data=idea.model_dump(mode="json")
        if origin=="material" and not grounded:return
        data.update(quote=data["quote"] if grounded else None,origin=origin,condition=condition)
        ideas.append(data)
    for item in explicit_ideas(scene,config,earliest,scene.sources,earliest):
        add(item,"material")
        if len(ideas)>=2:break
    for raw in proposals:
        if len(ideas)>=2:
            break
        origin="model"
        if isinstance(raw,dict):
            if reviewed and raw.get("origin") in {"calendar","route","material"}:
                origin=raw["origin"]
            raw={k:v for k,v in raw.items() if k in ComparisonIdea.model_fields}
        add(raw,origin)
    if ideas:
        return ideas
    watch=next(iter(watches),None)
    names={a.id:a.name for a in scene.entities}
    for event in scene.scheduled_events:
        if earliest<event.window<=config.windows:
            split=max(earliest,event.window-2)
            add({"question":"如果这项外部事件晚一窗发生，会有什么变化？","reason":event.text[:300],
                 "experiment":{"kind":"event_time","window":split,"through":min(config.windows,event.window+2),
                               "event_move":{"event_id":event.id,"new_window":event.window+1}},"watch":watch},"calendar")
            if ideas:break
    for route in scene.mail_routes:
        if route.transit_windows and max(earliest,route.active_from-1)<config.windows:
            split=max(earliest,route.active_from-1)
            add({"question":f"如果{names[route.source][:44]}寄给{names[route.target][:44]}的信提前一窗送到呢？",
                 "reason":"保持寄件人、收件人和原有权限，只调整新发件的运送时间。",
                 "experiment":{"kind":"transit","window":split,"through":min(config.windows,split+route.transit_windows+1),
                    "route":{"source":route.source,"target":route.target,"transit_windows":route.transit_windows-1}},"watch":watch},"route")
            if len(ideas)>=2 or (ideas and ideas[-1]["origin"]=="route"):break
    return ideas
