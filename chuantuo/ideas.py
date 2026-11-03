"""Turn a research question into an editable alternative, separate from the simulated world."""
import re
from pydantic import Field

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


def _grounded_quote(quote,scene):
    source=next((s for s in scene.sources if s.id==quote.source_id),None) if quote else None
    if not source:
        return False
    if quote.page is None:
        return quote.quote in source.text
    return any(p.number==quote.page and quote.quote in source.text[p.text_start:p.text_end] for p in source.pages)


def _explicit_date_question(scene,config,earliest):
    move=re.compile(r"(?:从|由|原定)(?:在)?\s*第?\s*(\d{1,6})\s*窗(?:口)?\s*(?:推迟|延后|延迟|提前|挪|移|调整|改)(?:到|至|为)\s*第?\s*(\d{1,6})\s*窗")
    ordinary={"如果","同一","一次","原定","计划","安排","事件","日期","发生","窗口","提前","推迟","这个","这项","时候"}
    for source in scene.sources:
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


def useful_ideas(scene,config,proposals=(),watches=(),*,earliest=0,reviewed=False):
    ideas=[]
    def add(item,origin):
        try:
            idea=ComparisonIdea.model_validate(item)
            spec=idea.experiment
            if spec.window<earliest or spec.through>config.windows or spec.kind=="repeat":
                return
            protect_credentials(idea.model_dump(mode="json"),config.catalog.values())
            if idea.watch:
                try:
                    idea.watch.resolve(scene,config.catalog.values())
                except ValueError:
                    idea=idea.model_copy(update={"watch":None})
            _,condition=apply_intervention(scene,spec,"proposed",scene.initial_state() if spec.window==0 else None,config.catalog.values())
            grounded=_grounded_quote(idea.quote,scene)
        except ValueError:
            return
        snapshot=spec.model_dump(mode="json")
        if any(row["experiment"]==snapshot or (spec.event_move and row["experiment"].get("event_move")==snapshot["event_move"]) for row in ideas):
            return
        data=idea.model_dump(mode="json")
        data.update(quote=data["quote"] if grounded else None,origin=origin,condition=condition)
        ideas.append(data)
    for item in _explicit_date_question(scene,config,earliest):
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
