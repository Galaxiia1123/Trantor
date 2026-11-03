"""Compare what one participant could see and what it actually did on two related routes."""
import json


def common_start(left,right):
    if left.run_id==right.run_id:raise ValueError("请选择两条不同的路线")
    a,b=left.experiment or {},right.experiment or {}
    if b.get("parent_id")==left.run_id:return b["window"]
    if a.get("parent_id")==right.run_id:return a["window"]
    if a and b and all(a[k]==b[k] for k in ("parent_id","parent_event","state_sha256","window")):return a["window"]
    raise ValueError("请选同一分岔点的两条路线，或一条原路线和它的直接分支")


def _line(value):
    return "未知" if value is None else "是" if value is True else "否" if value is False else str(value)


def field_contrast(left,right,actor,field,through=None):
    split=common_start(left,right)
    last=min(left.window,right.window)
    end=last if through is None else through
    if not split<=end<=last:raise ValueError("请选择两条路线都已结算的时点")
    a=next((a for a in left._scenario.entities if a.id==actor),None)
    f=next((f for f in a.fields if f.id==field),None) if a else None
    if not f:raise ValueError("这两条路线没有这个共同字段")
    counts={"equal":0,"different":0,"unknown":0}
    first=None;at_end=None
    opening=None
    if split==0 and any((sim.experiment or {}).get("kind")=="opening" for sim in (left,right)):
        initial=[sim._scenario.initial_state()[actor][field] for sim in (left,right)]
        kind="unknown" if None in initial else "equal" if initial[0]==initial[1] else "different"
        opening={"window":0,"kind":kind,"values":[_line(v)+((f.unit or "") if v is not None else "") for v in initial],
                 "records":["e000001","e000001"]}
        at_end=opening
    for w in range(split+1,end+1):
        records=[sim.journal.settlement(w) for sim in (left,right)]
        pair=[c["data"]["state"][actor][field] for c in records]
        kind="unknown" if None in pair else "equal" if pair[0]==pair[1] else "different"
        counts[kind]+=1
        at_end={"window":w,"kind":kind,"values":[_line(v)+((f.unit or "") if v is not None else "") for v in pair],
                "records":[c["id"] for c in records]}
        if kind=="different" and first is None:first=at_end
    return {"actor":a.name,"actor_id":actor,"field":f.label or f.id,"field_id":field,
            "after":split,"through":end,"counts":counts,"first":first,"end":at_end,**({"opening":opening} if opening else {})}


def _samples(rows):
    result={}
    for key,label in rows:
        key=json.dumps(key,sort_keys=True,ensure_ascii=False,separators=(",",":"))
        count=result.get(key,(0,None))[0]
        result[key]=(count+1,label)
    return result


def _sight(view):
    names={r["id"]:r["name"] for r in view["roster"]}
    fields=(((owner,fid,f["value"],f.get("unit")),names.get(owner,owner)+" · "+(f.get("label") or fid)+"为"+
             _line(f["value"])+(f.get("unit") or "")) for owner,fs in view["visible_state"].items() for fid,f in fs.items())
    letters=view["messages"]
    envelopes=(((m["from"],sorted((d["id"],d.get("sha256","")) for d in m.get("enclosures",()))),
                names.get(m["from"],m["from"])+"来信 · "+("附"+"、".join(d["title"] for d in m["enclosures"]) if m.get("enclosures") else "仅有文字")) for m in letters)
    text=(((m["from"]," ".join(m["text"].split())),names.get(m["from"],m["from"])+"："+m["text"]) for m in letters)
    docs=(((d["id"],d.get("received_window",0)),d["title"]+" · "+
          (f'第 {d["received_window"]} 窗首次取得' if d.get("received_window") else "开局持有")) for d in view.get("documents",()))
    notices=(((e["text"],e.get("at_settlement",[])),e["text"]+" "+"、".join(_line(a["value"]) for a in e.get("at_settlement",()))) for e in view.get("external_events",()))
    routes=(((r["source"],r["target"],r["transit_windows"]),names.get(r["source"],r["source"])+" → "+
            names.get(r["target"],r["target"])+f' · {r["transit_windows"]} 窗') for r in view.get("mail_routes",()))
    feedback=(((c["entity_id"],c["field_id"],c["before"],c["after"]),names.get(c["entity_id"],c["entity_id"])+" · "+
               (view["visible_state"].get(c["entity_id"],{}).get(c["field_id"],{}).get("label") or c["field_id"])+"　"+
               _line(c["before"])+" → "+_line(c["after"])) for c in (view.get("world_feedback") or {}).get("visible_changes",()))
    return {key:_samples(items) for key,items in [("可见字段",fields),("收到的信封与附件",envelopes),("来信原文",text),
            ("持有的文书",docs),("实际收到的外部通知",notices),("获知的结算变化",feedback),("已知通信时效",routes),
            ("已知规则",(((r["text"],r.get("active_until")),r["text"]+(f' · 至第 {r["active_until"]} 窗' if r.get("active_until") else "")) for r in view["rules"])),
            ("本轮到期的条件",((r["text"],r["text"]) for r in view.get("ended_conditions",()))),
            ("可见资料",(((m["owner_id"],m["domain"],m["text"]),m["text"]) for m in view["visible_materials"]))]}


def _action(action):
    d=action["data"]
    plan=d.get("next_review_window") or (d.get("review_at") if action["kind"]=="decision_deferred" else None)
    kind="沿用等待" if action["kind"]=="decision_deferred" else "作出决定"
    outgoing=(((sorted(set(m["recipients"])),sorted(m.get("enclosures",()))),
                "发给 "+"、".join(m["recipients"])+(" · 附 "+"、".join(m["enclosures"]) if m.get("enclosures") else " · 仅有文字"))
              for m in d.get("messages",()))
    return {"行动或等待":_samples([((kind,plan),kind+(f" · 第 {plan} 窗复看" if plan else ""))]),
            "发信对象与附件":_samples(outgoing)}


def _wording(action):
    d=action["data"]
    return {"决定原文":_samples([(" ".join(d["text"].split()),d["text"])]) if "text" in d else {},
            "发信原文":_samples(((sorted(set(m["recipients"]))," ".join(m["text"].split())),m["text"]) for m in d.get("messages",()))}


def _differences(left,right):
    result=[]
    for label,first in left.items():
        other=right[label]
        if first==other:continue
        def extra(a,b):
            entries=[(v[0]-b.get(k,(0,None))[0],v[1]) for k,v in a.items() if v[0]>b.get(k,(0,None))[0]]
            return {"count":sum(n for n,_ in entries),
                    "examples":[text[:200]+("…" if len(text)>200 else "")+(f"（{n} 次）" if n>1 else "") for n,text in entries[:3]]}
        a,b=extra(first,other),extra(other,first)
        if a["count"] or b["count"]:result.append({"category":label,"left":a,"right":b})
    return result


def actor_contrast(left,right,actor,through=None):
    split=common_start(left,right)
    if actor not in left.depths or actor not in right.depths:raise ValueError("两条路线都需要存在这个主体")
    last=min(left.window,right.window)
    end=last if through is None else through
    if not split<=end<=last:raise ValueError("请选择两条路线都已结算的时点")
    found=dict.fromkeys(("perception","behavior","wording"))
    for w in range(split+1,end+1):
        rounds=[]
        for sim in (left,right):
            rows,_=sim.journal.window_rows(w,0,actor=actor)
            rounds.append({e["data"]["round"]:e for e in rows if e["kind"] in {"expression","decision_deferred"} and e["id"] not in sim._discarded})
        if rounds[0].keys()!=rounds[1].keys():raise ValueError("已提交路线中的主体轮次不匹配")
        for turn,a in sorted(rounds[0].items()):
            b=rounds[1][turn]
            obs=[s.journal.observation_before(e,s._discarded) for s,e in ((left,a),(right,b))]
            if not all(obs):raise ValueError("这轮缺少当事主体的观察")
            for key,project,x,y in (("perception",_sight,obs[0]["data"]["visible"],obs[1]["data"]["visible"]),
                                    ("behavior",_action,a,b),("wording",_wording,a,b)):
                if found[key] is not None:continue
                changes=_differences(project(x),project(y))
                if changes:found[key]={"window":w,"round":turn,"groups":changes,"left":{"action":a["id"],"observation":obs[0]["id"]},
                                       "right":{"action":b["id"],"observation":obs[1]["id"]}}
            if all(found.values()):break
        if all(found.values()):break
    return {"split":split,"through":end,"actor_id":actor,"left":left.run_id,"right":right.run_id,**found}
