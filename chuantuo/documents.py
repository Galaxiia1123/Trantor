"""Named copies of operator-defined material. Possession changes only upon actual delivery."""
from hashlib import sha256

from .schema import message_scope


def envelope_scope(data, limits):
    parts=[data["text"]]
    for doc in data.get("enclosures",()):
        parts.extend((doc["title"],doc["text"]))
    return {term:readers for text in parts for term,readers in message_scope(text,limits).items()}


class Documents:
    def __init__(self, scene, limits):
        self.originals={}
        self.initial={}
        self.initial_by_actor={}
        self.allowed={}
        self.actors={a.id for a in scene.entities}
        for actor in scene.entities:
            for material in actor.materials:
                if not material.document:
                    continue
                doc=material.document
                original={"id":doc.id,"title":doc.title,"origin":actor.id,"text":material.text,
                          "sha256":sha256(material.text.encode("utf-8")).hexdigest()}
                self.originals[doc.id]=original
                owners=self.actors.copy() if material.visibility.public else {actor.id,*material.visibility.readers}
                self.initial[doc.id]=owners
                for holder in owners:
                    self.initial_by_actor.setdefault(holder,[]).append(doc.id)
                scope=material.sharing
                if scope is None:
                    recipients=owners.copy()
                else:
                    recipients=self.actors.copy() if scope.allowed_recipients is None else {actor.id,*scope.allowed_recipients}
                    recipients.difference_update(scope.excluded_recipients)
                for readers in message_scope(doc.title+" "+material.text,limits).values():
                    recipients.intersection_update(readers)
                self.allowed[doc.id]=recipients
        self.reset()

    def reset(self):
        self.copies={}

    def cabinet(self, actor):
        papers=[]
        received=self.copies.get(actor,{})
        for key in [*self.initial_by_actor.get(actor,()),*received]:
            orig=self.originals[key]
            receipt=received.get(key)
            allowed=self.allowed[key]
            papers.append({"id":key,"title":orig["title"],"origin":orig["origin"],
                           "can_send_to":"all" if allowed==self.actors else sorted(allowed),
                           **({"first_delivery":receipt["id"],"received_window":receipt["window"]} if receipt else {})})
        return papers

    def validate(self, decision, visible):
        held={d["id"] for d in visible.get("documents",())}
        for m in decision.messages:
            for key in m.enclosures:
                if key not in held:
                    raise ValueError("附件必须是你在本轮观察中已持有的原件。请从 documents 选择编号，或移除附件")
                if not set(m.recipients)<=self.allowed[key]:
                    raise ValueError(f"附件 {key} 的流转范围不包含这封信的全部收件人，请核对 can_send_to")

    def envelope(self, keys):
        return [dict(self.originals[key]) for key in keys]

    def received(self, event):
        data=event["data"]
        if not data.get("enclosures"):
            return data
        for doc in data["enclosures"]:
            key=doc["id"]
            if data["to"] not in self.initial[key]:
                self.copies.setdefault(data["to"],{}).setdefault(key,{"id":event["id"],"window":event["window"]})
        return {**data,"receipt_id":event["id"]}


def document_history(sim, through=None, document=None, offset=0, limit=30):
    with sim._lock:
        end=sim.window if through is None else through
        if not 0<=end<=sim.window:
            raise ValueError("请选择开局或已提交窗口")
        desk=sim._perception.documents
        names={a.id:a.name for a in sim._scenario.entities}
        catalog=[{"id":doc["id"],"title":doc["title"],"origin":names[doc["origin"]]} for doc in desk.originals.values()]
        key=document or next(iter(desk.originals),None)
        if key is None:
            return {"documents":catalog,"selected":None,"through":end,"items":[]}
        if key not in desk.originals:
            raise ValueError("没有这份登记原件")
        holders={a:{"actor":a,"name":names[a],"window":0,"record":"e000001"} for a in names if a in desk.initial[key]}
        copies={}
        for window in range(1,end+1):
            for kind in ("message_dispatched","message_delivered"):
                rows,_=sim.journal.window_rows(window,0,kind=kind)
                for event in rows:
                    d=event["data"]
                    if event["id"] in sim._discarded or not any(doc["id"]==key for doc in d.get("enclosures",())):
                        continue
                    link=d.get("dispatch_id",event["id"])
                    if link not in copies:
                        action=sim.journal.event(d["action_id"])
                        copies[link]={"from":d["from"],"to":d["to"],"sender":names[d["from"]],"recipient":names[d["to"]],
                                      "sent_window":action["window"],"sent_round":action["data"]["round"],"action":action["id"],
                                      "due_window":d.get("due_window",window),"dispatch":link if kind=="message_dispatched" else None,
                                      "receipt":None,"arrived_window":None}
                    if kind=="message_delivered":
                        copies[link].update(receipt=event["id"],arrived_window=window)
                        holders.setdefault(d["to"],{"actor":d["to"],"name":names[d["to"]],"window":window,"record":event["id"]})
        ordered=sorted(copies.values(),key=lambda m:(int(m["action"][1:]),int((m["dispatch"] or m["receipt"])[1:])),reverse=True)
        return {"documents":catalog,"through":end,"selected":desk.originals[key],"holders":list(holders.values()),
                "permitted":[names[a] for a in names if a in desk.allowed[key]],"total":len(ordered),
                "items":ordered[offset:offset+limit],"next":min(offset+limit,len(ordered))}
