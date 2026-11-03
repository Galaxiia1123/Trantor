"""Navigate a long run and read the evidence attached to one settled field."""

def _window_mark(sim, window):
    cached=sim._window_marks.get(window)
    if cached:
        return cached
    commit=sim.journal.settlement(window)
    data=commit["data"]
    rows,_=sim.journal.window_rows(window,0)
    delivered=external=0
    for e in rows:
        if e["id"] not in sim._discarded:
            delivered += e["kind"]=="message_delivered"
            external += e["kind"]=="external_event"
    mark={"changes":len(data["changes"]),"messages":delivered,"external":external}
    sim._window_marks[window]=mark
    return mark


def window_strip(sim, start=1, through=None, slots=32):
    with sim._lock:
        end=sim.window if through is None else through
        if end==0 and start==1:
            return {"start":1,"through":0,"slots":[],"changed_windows":0,"first_change":None,"last_change":None}
        if not 1<=start<=end<=sim.window:
            raise ValueError("只能查看已经提交的窗口")
        width=(end-start+slots)//slots
        bins=[]
        changed_windows=0
        first=last=None
        for low in range(start,end+1,width):
            high=min(end,low+width-1)
            item={"start":low,"end":high,"changes":0,"messages":0,"external":0}
            for w in range(low,high+1):
                mark=_window_mark(sim,w)
                for key,value in mark.items():
                    item[key]+=value
                if mark["changes"]:
                    changed_windows+=1
                    if first is None:
                        first=w
                    last=w
            bins.append(item)
        return {"start":start,"through":end,"slots":bins,"changed_windows":changed_windows,"first_change":first,"last_change":last}


def _evidence_row(sim, ref, names):
    e=sim.journal.event(ref)
    d=e["data"]
    row={"id":ref,"kind":e["kind"],"window":e["window"],"text":d.get("text","")}
    if e["kind"]=="expression":
        row.update(actor_id=d["actor_id"],actor=names[d["actor_id"]],round=d["round"])
        observation=sim.journal.inbox_before(e,sim._discarded)
        if observation:
            obs_id,count,inbox=observation
            row.update(observation=obs_id,inbox_count=count,
                       inbox=[{"from":names[m["from"]],"text":m["text"][:260],"action":m["action_id"]} for m in inbox])
    elif e["kind"]=="message_delivered":
        row.update(sender=names[d["from"]],recipient=names[d["to"]],dispatch=d.get("dispatch_id"),action=d["action_id"])
        if d.get("dispatch_id"):
            row.update(sent_window=d["sent_window"],seen=sim.journal.observation_receipt(d["to"],e["window"],1,d,sim._discarded),
                       recipient_id=d["to"])
    elif e["kind"]=="external_event":
        row["provenance"]=d.get("provenance",{})
    return row


def field_reason(sim, window, actor_id, field_id, offset=0, limit=12):
    with sim._lock:
        if not 0<=window<=sim.window:
            raise ValueError("这个窗口尚未结算")
        actor=next((a for a in sim._scenario.entities if a.id==actor_id),None)
        field=next((f for f in actor.fields if f.id==field_id),None) if actor else None
        if field is None:
            raise ValueError("场景中没有这个主体字段")
        names={a.id:a.name for a in sim._scenario.entities}
        result={"window":window,"actor_id":actor_id,"actor":actor.name,"field_id":field_id,"label":field.label or field.id,
                "unit":field.unit,"kind":field.kind,"definition":field.provenance.model_dump(mode="json",exclude_defaults=True)}
        commit=sim.journal.settlement(window) if window else None
        if commit:
            review=next(v for v in commit["data"]["reviews"] if (v["entity_id"],v["field_id"])==(actor_id,field_id))
            before,after=review["before"],review["after"]
            refs=list(dict.fromkeys([*review.get("event_ids",()),*review["action_ids"]]))
            result.update(record=commit["id"],reason=review["reason"],total=len(refs),next=min(offset+limit,len(refs)),
                          evidence=[_evidence_row(sim,e,names) for e in refs[offset:offset+limit]],
                          influences=[{**r,"from_name":names[r["source"]],"to_name":names[r["target"]]} for r in review.get("influence_refs",())])
        else:
            before=after=field.value
            result.update(record="e000001",reason=field.provenance.explanation or "开局采用这项字段设定。",total=0,next=0,evidence=[],influences=[])
        prior=next((w for w in range(window-1,0,-1) if any((r["entity_id"],r["field_id"])==(actor_id,field_id)
                   for r in sim.journal.settlement(w)["data"]["changes"])),None)
        return {**result,"before":before,"after":after,"changed":before!=after,"previous_change":prior,
                "exact_before":str(before) if field.kind=="number" and before is not None else None,
                "exact_after":str(after) if field.kind=="number" and after is not None else None}
