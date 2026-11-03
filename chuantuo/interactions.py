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
            seen=sim.journal.observation_receipt(data["to"],next_w,next_turn,data,sim._discarded) if next_w<=end else None
            if seen: stage="observed"
            elif next_w<=end: stage="unverified"
            elif next_w<=sim.window and next_w>end: stage="beyond_range"
            elif next_w>sim.window and sim.status in {"stopped","completed"}: stage="no_next_observation"
            else: stage="awaiting_observation"
            items.append({"receipt":event["id"],"action":action["id"],"window":w,"round":turn,"from":data["from"],"to":data["to"],
                          "text":data["text"],"stage":stage,"seen":{"record":seen,"window":next_w,"round":next_turn} if seen else None})
        return {"start":start,"through":end,"total":len(matches),"next":offset+len(items),"items":items}
