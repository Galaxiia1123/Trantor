"""Project the committed delivery ledger into focused links and verifiable conversation threads."""
from collections import Counter


def _range(sim, start, through):
    end=sim.window if through is None else through
    if not 0<=start<=end<=sim.window:
        raise ValueError("请在开局与已提交窗口之间选择通信时段")
    return end


def _deliveries(sim,start,end):
    for w in range(max(1,start),end+1):
        rows,_=sim.journal.window_rows(w,0,kind="message_delivered")
        for event in rows:
            if event["id"] not in sim._discarded:
                yield event


def interaction_map(sim,start=0,through=None,focus=None):
    with sim._lock:
        end=_range(sim,start,through)
        if focus and focus not in sim.depths:
            raise ValueError("找不到所选主体")
        sent,received,pairs=Counter(),Counter(),Counter()
        examples={}
        for e in _deliveries(sim,start,end):
            sender,target=e["data"]["from"],e["data"]["to"]
            pair=(sender,target)
            sent[sender]+=1;received[target]+=1;pairs[pair]+=1
            if pair not in examples: examples[pair]=[e["id"]]
            elif len(examples[pair])==1: examples[pair].append(e["id"])
            else: examples[pair][-1]=e["id"]
        if not focus:
            focus=max(sim.depths,key=lambda a:sent[a]+received[a])
        routes=[{"source":a,"target":b,"count":n,"event_ids":examples[a,b]} for (a,b),n in pairs.items() if focus in (a,b)]
        return {"start":start,"through":end,"focus":focus,"total":sum(pairs.values()),"directions":len(pairs),
                "cross_layer":sum(n for (a,b),n in pairs.items() if sim.depths[a]!=sim.depths[b]),
                "actors":[{"id":a.id,"name":a.name,"depth":sim.depths[a.id],"level":sim.depths[a.id]+1,"sent":sent[a.id],"received":received[a.id]} for a in sim._scenario.entities],
                "influences":[e.model_dump(exclude={"provenance"}) for e in sim._scenario.influences if focus in (e.source,e.target)],
                "routes":routes}


def conversation(sim,one,two,start=0,through=None,query="",offset=0,limit=30):
    with sim._lock:
        end=_range(sim,start,through)
        if one not in sim.depths or two not in sim.depths:
            raise ValueError("请选择场景中的通信双方")
        query=query.strip().casefold()
        matches=[]
        for event in _deliveries(sim,start,end):
            data=event["data"]
            if (data["from"],data["to"]) in ((one,two),(two,one)) and (not query or query in data["text"].casefold()):
                matches.append(event)
        items=[]
        for event in matches[offset:offset+limit]:
            data=event["data"];w=event["window"]
            action=sim.journal.event(data["action_id"]);turn=action["data"]["round"]
            next_w,next_turn=(w,turn+1) if turn<sim.config.rounds_per_window else (w+1,1)
            if data.get("dispatch_id"):
                next_w,next_turn=w,1
            seen=sim.journal.observation_receipt(data["to"],next_w,next_turn,data,sim._discarded) if next_w<=end else None
            if seen: stage="observed"
            elif next_w<=end: stage="unverified"
            elif next_w<=sim.window and next_w>end: stage="beyond_range"
            elif next_w>sim.window and sim.status in {"stopped","completed"}: stage="no_next_observation"
            else: stage="awaiting_observation"
            items.append({"receipt":event["id"],"action":action["id"],"window":action["window"],"arrived_window":w,"dispatch":data.get("dispatch_id"),
                          "round":turn,"from":data["from"],"to":data["to"],"reply_to":data.get("reply_to"),
                          "has_replies":sim.journal.has_replies(event["id"],end,sim._discarded),
                          "text":data["text"],"stage":stage,"seen":{"record":seen,"window":next_w,"round":next_turn} if seen else None,
                          **({"enclosures":[{"id":d["id"],"title":d["title"]} for d in data["enclosures"]]} if data.get("enclosures") else {})})
        return {"start":start,"through":end,"total":len(matches),"next":offset+len(items),"items":items}


def reply_chain(sim,receipt_id,through=None,offset=0,limit=30):
    with sim._lock:
        end=_range(sim,0,through);selected=sim.journal.event(receipt_id)
        if selected["kind"]!="message_delivered" or selected["window"]>end or selected["id"] in sim._discarded:
            raise ValueError("请选择此时已经送达的信件")
        pair={selected["data"]["from"],selected["data"]["to"]}
        roots={};depth={};envelopes=[];arrived=set()
        for w in range(1,end+1):
            rows,_=sim.journal.window_rows(w,0)
            for e in rows:
                if e["kind"] not in {"message_delivered","message_dispatched"} or e["id"] in sim._discarded:continue
                d=e["data"]
                if {d["from"],d["to"]}!=pair:continue
                if e["kind"]=="message_dispatched" and not d.get("reply_to"):continue
                parent=d.get("reply_to");key=e["id"]
                if parent and parent not in roots:raise ValueError("回复所指的原信没有对应签收记录")
                roots[key]=roots[parent] if parent else key;depth[key]=depth[parent]+1 if parent else 0
                if d.get("dispatch_id"):arrived.add(d["dispatch_id"])
                envelopes.append(e)
        root=roots[receipt_id]
        entries=[e for e in envelopes if roots[e["id"]]==root and e["id"] not in arrived]
        names={a.id:a.name for a in sim._scenario.entities if a.id in pair}
        items=[]
        for e in entries[offset:offset+limit]:
            d=e["data"];ref=e["id"];pending=e["kind"]=="message_dispatched"
            sent=sim.journal.event(d["action_id"])
            items.append({"id":ref,"parent":d.get("reply_to"),"depth":depth[ref],"sender":names[d["from"]],"recipient":names[d["to"]],
                          "sent_window":sent["window"],"round":sent["data"]["round"],"received_window":None if pending else e["window"],
                          "due_window":d.get("due_window"),"text":d["text"][:1400],"truncated":len(d["text"])>1400,
                          "enclosures":[doc["title"] for doc in d.get("enclosures",())]})
        return {"root":root,"selected":receipt_id,"through":end,"total":len(entries),"items":items,"next":offset+len(items)}


def dispatch_ledger(sim, through=None, actor=None, stage=None, offset=0, limit=30):
    """Reconstruct postal status at the selected boundary, using only receipts up to that point."""
    with sim._lock:
        end=_range(sim,0,through)
        if actor and actor not in sim.depths:
            raise ValueError("找不到所选主体")
        items={}
        for window in range(1,end+1):
            for kind in ("message_dispatched","message_delivered"):
                rows,_=sim.journal.window_rows(window,0,actor=actor,kind=kind)
                for e in rows:
                    d=e["data"]
                    if e["id"] in sim._discarded:
                        continue
                    if kind=="message_dispatched":
                        items[e["id"]]={"dispatch":e["id"],"receipt":None,"seen":None,"stage":"pending",**d}
                    elif ref:=d.get("dispatch_id"):
                        item=items[ref]
                        item.update(stage="arrived",receipt=e["id"])
        pending=sum(i["stage"]=="pending" for i in items.values())
        done=end==sim.window and sim.status in {"completed","stopped"}
        ordered=sorted((i for i in items.values() if stage is None or i["stage"]==stage),
                       key=lambda i:(i["stage"]!="pending",i["due_window"] if i["stage"]=="pending" else -i["due_window"],i["dispatch"]))
        page=ordered[offset:offset+limit]
        for item in page:
            if item["receipt"]:
                envelope={**item,"dispatch_id":item["dispatch"]}
                item["seen"]=sim.journal.observation_receipt(item["to"],item["due_window"],1,envelope,sim._discarded)
        return {"through":end,"ended":done,"pending":pending,"arrived":len(items)-pending,"total":len(ordered),
                "items":page,"next":min(offset+limit,len(ordered)),
                "routes":sim._perception.known_routes(actor,max(1,end))}
