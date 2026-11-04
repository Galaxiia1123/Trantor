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


def actor_activity(sim, start=1, through=None, slots=24, search="", offset=0, limit=16):
    """Summarize committed behavior without ranking actors or interpreting their motives."""
    with sim._lock:
        end = sim.window if through is None else through
        if not 1 <= start <= end <= sim.window:
            raise ValueError("只能查看已经结算的主体动向")
        if not 2 <= slots <= 48 or not 0 <= offset or not 1 <= limit <= 40:
            raise ValueError("主体动向的范围不合法")
        query = search.strip().casefold()
        actors = [a for a in sim._scenario.entities if query in (a.name + " " + a.id).casefold()]
        selected = actors[offset:offset + limit]
        width = (end - start + slots) // slots
        bins = [{"start":low, "end":min(end, low + width - 1)} for low in range(start,end+1,width)]
        rows = {a.id:{"actor_id":a.id, "name":a.name, "depth":sim.depths[a.id],
                     "cells":[{**b, "changes":0, "sent":0, "received":0, "decisions":0, "waiting":0,
                               "last_active":None, "last_change":None} for b in bins]} for a in selected}
        for window in range(start,end+1):
            index = (window-start)//width
            events, _ = sim.journal.window_rows(window,0)
            for event in events:
                if event["id"] in sim._discarded:
                    continue
                data = event["data"]
                kind = event["kind"]
                actor = data.get("actor_id") if kind != "message_delivered" else data["to"]
                cell = rows[actor]["cells"][index] if actor in rows else None
                if cell is not None and kind in {"expression","decision_deferred","message_delivered"}:
                    cell["last_active"] = window
                    if kind == "expression":
                        cell["decisions"] += 1
                        cell["sent"] += sum(len(set(m["recipients"])) for m in data.get("messages",()))
                    elif kind == "decision_deferred":
                        cell["waiting"] += 1
                    else:
                        cell["received"] += 1
                if kind == "window_committed":
                    for change in data["changes"]:
                        if change["entity_id"] in rows:
                            affected = rows[change["entity_id"]]["cells"][index]
                            affected["changes"] += 1
                            affected["last_change"] = window
        return {"start":start, "through":end, "bins":bins, "actors":list(rows.values()),
                "total":len(actors), "offset":offset, "next":min(offset+limit,len(actors))}


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
        if d.get("enclosures"):
            row["enclosures"]=[doc["title"] for doc in d["enclosures"]]
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
        if period:=field.entry_period(after):
            result["entry_period"]=period.model_dump(mode="json")
        return {**result,"before":before,"after":after,"changed":before!=after,"previous_change":prior,
                "exact_before":str(before) if field.kind=="number" and before is not None else None,
                "exact_after":str(after) if field.kind=="number" and after is not None else None}
