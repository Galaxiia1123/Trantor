"""The chronology of one participant's actual observations and choices."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from hashlib import sha256

from .perception import state_sentences


def _recorded_moment(sim, actor_id, window, turn):
    if actor_id not in sim.depths or not 1 <= window <= sim.window:
        raise ValueError("请选择已结算窗口中的主体")
    rows,_=sim.journal.window_rows(window,0,actor=actor_id)
    action=next((e for e in rows if e["kind"] in {"expression","decision_deferred"} and
                 e["data"]["round"]==turn and e["id"] not in sim._discarded),None)
    obs=sim.journal.observation_before(action,sim._discarded) if action else None
    if not obs:
        raise ValueError("该时点没有这个主体的已提交决定")
    return action,obs


def held_paper(sim, actor_id, document_id, window, turn):
    with sim._lock:
        _,obs=_recorded_moment(sim,actor_id,window,turn)
        view=obs["data"]["visible"]
        paper=next((d for d in view.get("documents",()) if d["id"]==document_id),None)
        if paper is None:
            raise ValueError("这个主体在该时点没有持有这份文书")
        if receipt_id:=paper.get("first_delivery"):
            receipt=sim.journal.event(receipt_id)
            if (receipt["kind"]!="message_delivered" or receipt["data"]["to"]!=actor_id or receipt_id in sim._discarded
                    or int(receipt_id[1:])>=int(obs["id"][1:])):
                raise ValueError("该主体持有的文书与首次签收记录不符")
            doc=next((d for d in receipt["data"].get("enclosures",()) if d["id"]==document_id),None)
            if not doc or sha256(doc["text"].encode()).hexdigest()!=doc["sha256"]:
                raise ValueError("该签收记录中缺少匹配的文书正文")
            return {"title":doc["title"],"text":doc["text"],"record":receipt_id,"acquired":receipt["window"]}
        material=next((m for m in view["visible_materials"] if (m.get("document") or {}).get("id")==document_id),None)
        if not material:
            raise ValueError("该主体的开局可见资料缺少这份原件")
        return {"title":paper["title"],"text":material["text"],"record":obs["id"],"acquired":0}


def _rounds(sim, actor_id, through):
    turns = []
    last_feedback = None
    for window in range(1, through+1):
        rows, _ = sim.journal.window_rows(window, 0, actor=actor_id)
        for action in rows:
            if action["kind"] not in {"expression","decision_deferred"} or action["id"] in sim._discarded:
                continue
            observation = sim.journal.observation_before(action, sim._discarded)
            if not observation:
                raise ValueError("这轮决定缺少当事主体的原始观察")
            visible = observation["data"]["visible"]
            feedback = visible.get("world_feedback") or {}
            changed = bool(feedback) and feedback["commit_id"] != last_feedback
            if feedback:
                last_feedback = feedback["commit_id"]
            news = bool(not turns or changed or visible["messages"] or visible.get("external_events") or visible.get("ended_conditions"))
            turns.append((action, observation, changed, news))
    return turns


def _position(item):
    return {"window":item[0]["window"], "round":item[0]["data"]["round"]}


def _letters(action, observation, direction):
    return observation["data"]["visible"]["messages"] if direction=="inbox" else action["data"].get("messages",())


def _letter(message, direction, names, clip=None):
    text = message["text"]
    row = {"text":text if clip is None else text[:clip], "truncated":clip is not None and len(text)>clip}
    if direction == "outbox":
        return {**row,"to":[names.get(a,a) for a in message["recipients"]], "enclosures":message.get("enclosures",[])}
    return {**row,"sender":names.get(message["from"],message["from"]),
            "enclosures":[{"id":d["id"],"title":d["title"]} for d in message.get("enclosures",())],
            **({"dispatched":message["sent_window"],"arrived":message.get("arrived_window",message["due_window"])} if message.get("dispatch_id") else {})}


def letter_at(sim, actor_id, window, turn, direction, index):
    with sim._lock:
        action,obs=_recorded_moment(sim,actor_id,window,turn)
        letters=_letters(action,obs,direction)
        if not 0<=index<len(letters):
            raise ValueError("所选收发记录不在这个轮次")
        names={a["id"]:a["name"] for a in obs["data"]["visible"]["roster"]}
        return {"index":index,"total":len(letters),"letter":_letter(letters[index],direction,names)}


def _moment(item):
    action, observation, changed, _ = item
    visible, choice = observation["data"]["visible"], action["data"]
    # All labels and values below come from this participant's observation, including the roster.
    names = {a["id"]:a["name"] for a in visible["roster"]}
    def name(actor):
        return names.get(actor, actor)
    inbox, outbox = (_letters(action,observation,d) for d in ("inbox","outbox"))
    holdings = [{k:d[k] for k in ("id","title","received_window") if k in d} for d in visible.get("documents",())]
    self_state = state_sentences(visible["visible_state"]).get(choice["actor_id"])
    changes = []
    if changed:
        for c in visible["world_feedback"]["visible_changes"]:
            f = visible["visible_state"][c["entity_id"]][c["field_id"]]
            changes.append({"owner":name(c["entity_id"]), "label":f.get("label") or c["field_id"],
                            "before":c["before"], "after":c["after"], "unit":f.get("unit")})
    return {**_position(item), "action":action["id"], "observation":observation["id"], "deferred":action["kind"]=="decision_deferred",
            "text":choice.get("text"), "review_at":choice.get("review_at"), "own_state":self_state,
            "inbox":[_letter(m,"inbox",names,300) for m in inbox[:5]], "inbox_total":len(inbox),
            "outbox":[_letter(m,"outbox",names,300) for m in outbox[:5]], "outbox_total":len(outbox),
            "holdings":holdings, "changes":changes,
            "notices":[e["text"] for e in visible.get("external_events",())],
            "ended":[r["text"] for r in visible.get("ended_conditions",())]}


def journey(sim, actor_id, window=None, turn=None, news_only=False, through=None):
    with sim._lock:
        if actor_id not in sim.depths:
            raise ValueError("该路线中没有这个主体")
        end = sim.window if through is None else through
        if not 1 <= end <= sim.window or (window is not None and not 1 <= window <= end):
            raise ValueError("请选择已结算窗口")
        turns = _rounds(sim, actor_id, end)
        if not turns:
            raise ValueError("这个范围内没有已提交的主体决定")
        positions = [(s[0]["window"],s[0]["data"]["round"]) for s in turns]
        i = bisect_left(positions, (window or 1,turn or 1))
        if i == len(turns) or positions[i] != (window or 1,turn or 1):
            raise ValueError("这个主体在该时点没有已提交决定")
        news = [j for j,s in enumerate(turns) if s[3]]
        navigation = news if news_only else range(len(turns))
        before, after = bisect_left(navigation,i), bisect_right(navigation,i)
        current=turns[i]
        who=current[1]["data"]["visible"]["self"]
        return {"run_id":sim.run_id, "actor":{"id":who["id"],"name":who["name"]}, "through":end,
                "total":len(turns),"with_news":len(news), "ordinal":i+1, "current":_moment(current),
                "previous":_position(turns[navigation[before-1]]) if before else None,
                "next":_position(turns[navigation[after]]) if after<len(navigation) else None,
                "first":_position(turns[0]), "last":_position(turns[-1]),
                "rounds_per_window":sim.config.rounds_per_window, "supports_experiment":getattr(sim._scenario,"format_version","1.0")=="1.1",
                "fork_window":(sim.experiment or {}).get("window",0),"parent_id":(sim.experiment or {}).get("parent_id")}
