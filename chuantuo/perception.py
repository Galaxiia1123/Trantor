"""Build actor-local inputs from the frozen scenario and the latest committed state."""
from collections import defaultdict
from copy import deepcopy
from heapq import merge

from .schema import Relation, Scenario11, ScopedRule, message_scope
from .documents import Documents


def state_sentences(visible_fields):
    profiles = {}
    for owner, fields in visible_fields.items():
        facts = []
        for name, field in fields.items():
            value = field["value"]
            if value is None:
                shown = "未知"
            elif type(value) is bool:
                shown = "是" if value else "否"
            else:
                shown = str(value) + (field.get("unit") or "")
            label = field.get("label") or name
            if isinstance(value, str) and value.startswith(("已", "尚", "待", "正在", "暂", "等待")):
                line = label + shown
            else:
                line = f"{label}为{shown}"
            if rule := field.get("constraints"):
                limits = []
                if rule.get("minimum") is not None:
                    limits.append(f"下限{rule['minimum']}{field.get('unit') or ''}")
                if rule.get("maximum") is not None:
                    limits.append(f"上限{rule['maximum']}{field.get('unit') or ''}")
                if rule.get("fixed"):
                    limits.append("固定值")
                if limits:
                    line += "（" + "，".join(limits) + "）"
            facts.append(line)
        if facts:
            profiles[owner] = "，".join(facts) + "。"
    return profiles


def decision_view(observation):
    view = {k: v for k, v in observation.items() if k not in {"visible_state", "roster_scope", "world_feedback"}}
    view["state"] = state_sentences(observation["visible_state"])
    definitions=[]
    for owner,fields in observation["visible_state"].items():
        for fid,field in fields.items():
            rules=field.get("constraints") or {}
            if rules.get("choices"):
                definitions.append({"owner":owner,"field":fid,"label":field.get("label") or fid,
                                    **{k:rules[k] for k in ("choices","meanings","entry_windows") if rules.get(k)}})
    if definitions:view["field_definitions"]=definitions
    if routes:=observation.get("mail_routes"):
        window=observation["window"]
        view["mail_timing"]="下面是本窗生效的线路时效，旧材料与来信中的预计日期只代表当时的说法。"
        view["mail_routes"]=[{**r,"本窗发件的送达时点":f"第 {window+r['transit_windows']} 窗开始" if r["transit_windows"] else "本轮结束"} for r in routes]
    if observation["messages"] and (view.get("documents") or any(m.get("dispatch_id") or m.get("enclosures") for m in observation["messages"])):
        view["messages"]=[]
        for message in observation["messages"]:
            when=f"第 {message.get('arrived_window',message.get('due_window'))} 窗首" if message.get("dispatch_id") else "上一轮结束时"
            attachments=[d["title"] for d in message.get("enclosures",())]
            contents="实际随信附件："+"、".join(attachments) if attachments else "本信只有文字，无随信附件"
            view["messages"].append({**message,"到件记录":when+"收到这封信。"+contents+"。"})
    if any(d.get("first_delivery") for d in view.get("documents",())):
        view["document_timing"]="已持有副本的 received_window 和 first_delivery 记录首次取得的窗口和凭据。之后收到文字说明或再次获得副本，都沿用首次取得时间。"
    if observation.get("external_events"):
        view["external_events"]=[{**e,"实际通知窗口":observation["window"]} for e in observation["external_events"]]
    if feedback := observation.get("world_feedback"):
        # Current values are already in the profile. Show which changed and when.
        view["changes"] = {"window": feedback["window"], "record": feedback["commit_id"],
                           "fields": [f"{c['entity_id']}.{c['field_id']}" for c in feedback["visible_changes"]]}
    return view


class Perception:
    def __init__(self, scenario, depths, config):
        self.scene = scenario
        self.depths = depths
        self.config = config
        self.delivery_limits = scenario.delivery_limits() if isinstance(scenario,Scenario11) else {}
        self.documents=Documents(scenario,self.delivery_limits)
        self.mail_routes = defaultdict(list)
        for route in sorted(getattr(scenario,"mail_routes",()),key=lambda r:r.active_from):
            self.mail_routes[route.source,route.target].append(route)
        self.actors = {actor.id: actor for actor in scenario.entities}
        self.calendar=defaultdict(list)
        for event in getattr(scenario,"scheduled_events",()):
            self.calendar[event.window].append(event)
        self.order = {actor_id: index for index, actor_id in enumerate(self.actors)}
        self.systems = {system.id: system for system in scenario.subsystems}
        self.contacts = {key: {key} for key in self.actors}
        self.edges = defaultdict(list)
        for entity in scenario.entities:
            parent = entity.parent_id or (self.systems[entity.subsystem_id].owner_id if entity.subsystem_id else None)
            if parent:
                self.contacts[entity.id].add(parent)
                self.contacts[parent].add(entity.id)
        for edge in scenario.influences:
            if isinstance(edge, Relation):
                item = {"source": edge.source, "target": edge.target, "description": edge.description,
                        "origin": edge.provenance.kind}
            else:
                item = {"source":edge.source, "target":edge.target, "weight":edge.weight,
                        "weight_source":edge.provenance.kind}
            for viewer in {edge.source, edge.target}:
                self.contacts[viewer].update((edge.source,edge.target))
                self.edges[viewer].append(item)
        self.public_fields, self.granted_fields = self._index("fields")
        self.public_materials, self.granted_materials = self._index("materials")

    def transit(self, source, target, window):
        routes=self.mail_routes.get((source,target),())
        return next((r.transit_windows for r in reversed(routes) if r.active_from<=window),0)

    def known_routes(self, actor_id, window):
        result=[]
        for (source,target),versions in self.mail_routes.items():
            if actor_id is not None and actor_id not in (source,target):
                continue
            route=next((r for r in reversed(versions) if r.active_from<=window),None)
            if route:
                result.append({"source":source,"target":target,"transit_windows":route.transit_windows})
        return result

    def _index(self, attribute):
        public, granted = [], defaultdict(list)
        position = 0
        for actor in self.scene.entities:
            for item in getattr(actor,attribute):
                entry = (position, actor.id, item)
                position += 1
                if item.visibility.public:
                    public.append(entry)
                else:
                    for viewer in {actor.id,*item.visibility.readers}:
                        granted[viewer].append(entry)
        return public, granted

    def observe(self, actor_id, state, inbox, window, turn, feedback, external=()):
        actor = self.actors[actor_id]
        fields, materials = {}, []
        for _, owner, field in merge(self.public_fields, self.granted_fields[actor_id]):
            fields.setdefault(owner,{})[field.id] = {"value":state[owner][field.id], "domain":field.domain,
                "kind":field.kind,"unit":field.unit,"provenance":{"kind":field.provenance.kind}}
            if field.constraints:
                fields[owner][field.id]["constraints"] = field.constraints.model_dump(exclude_defaults=True,exclude={
                    k:{"__all__":{"provenance"}} for k in ("entry_windows","meanings")})
            if field.label:
                fields[owner][field.id]["label"] = field.label
        for _, owner, material in merge(self.public_materials, self.granted_materials[actor_id]):
            info = {"owner_id":owner,"domain":material.domain,"text":material.text,"provenance":{"kind":material.provenance.kind}}
            if material.document:
                info["document"]=material.document.model_dump()
            if scope := message_scope(material.text,self.delivery_limits):
                info["sharing"] = scope
            materials.append(info)
        routes = self.known_routes(actor_id,window)
        if self.config.roster_policy == "neighbors":
            included = self.contacts[actor_id] | fields.keys() | {m["owner_id"] for m in materials} | {m["from"] for m in inbox}
            included.update(k for r in routes for k in (r["source"],r["target"]))
            systems = set()
            for member in tuple(included):
                current = self.actors[member]
                while current.subsystem_id or current.parent_id:
                    if current.subsystem_id:
                        systems.add(current.subsystem_id)
                    parent = current.parent_id or self.systems[current.subsystem_id].owner_id
                    included.add(parent)
                    current = self.actors[parent]
            roster = [self.actors[key] for key in sorted(included,key=self.order.__getitem__)]
            subsystems = [self.systems[key] for key in sorted(systems)]
        else:
            roster, subsystems = self.scene.entities, self.scene.subsystems
        result = {"window":window, "round":turn, "window_duration":self.config.window_duration,
            "background":self.scene.public_brief.text if getattr(self.scene,"public_brief",None) is not None else self.scene.background,
            "rules":[{"text":r.text,"provenance":{"kind":r.provenance.kind},
                      **({"active_until":r.active_until} if isinstance(r,ScopedRule) and r.active_until else {})} for r in self.scene.rules
                if not isinstance(r,ScopedRule) or
                (r.applies(window) and (r.visibility.public or actor_id in r.visibility.readers))],
            "self":{"id":actor.id,"name":actor.name,"identity":actor.identity,"preferences":actor.preferences,"depth":self.depths[actor_id]},
            "roster":[{"id":a.id,"name":a.name,"subsystem_id":a.subsystem_id, **({"parent_id":a.parent_id} if a.parent_id else {})} for a in roster],
            "subsystems":[s.model_dump(exclude={"interaction"}) if s.owner_id != actor_id and actor.subsystem_id != s.id else s.model_dump() for s in subsystems],
            "visible_state":fields, "visible_materials":materials, "influences":[dict(e) for e in self.edges[actor_id]], "messages":deepcopy(inbox)}
        if papers:=self.documents.cabinet(actor_id):
            result["documents"]=papers
        if turn==1:
            ended=[{"text":r.text,"last_window":r.active_until} for r in self.scene.rules if isinstance(r,ScopedRule) and
                   r.active_until==window-1 and (r.visibility.public or actor_id in r.visibility.readers)]
            if ended:result["ended_conditions"]=ended
        if routes:
            result["mail_routes"]=routes
            result["mail_timing"]="定向线路的 transit_windows 是从发出至到达的窗口数。大于零时在到达窗口的第一轮前交付；零表示本轮结束后交付。"
        if self.config.roster_policy == "neighbors":
            result["roster_scope"] = {"mode":"neighbors","total":len(self.actors),"included":len(roster),
                "note":"此表仅列与本主体有关的层级、直接关系、可见资料和收件来源。其他已知主体仍可作为定向收件人。"}
        if turn==1:
            notices=[]
            for event in external:
                data=event["data"]
                scope=data["visibility"]
                if scope["public"] or actor_id in scope["readers"]:
                    notice={"record":event["id"],"text":data["text"]}
                    visible_assignments=[effect for effect in data["assignments"] if effect["field_id"] in fields.get(effect["entity_id"],{})]
                    if visible_assignments: notice["at_settlement"]=deepcopy(visible_assignments)
                    notices.append(notice)
            if notices: result["external_events"]=notices
        if feedback:
            changes = [{k:change[k] for k in ("entity_id","field_id","before","after")}
                       for change in feedback["changes"] if change["field_id"] in fields.get(change["entity_id"],{})]
            if changes:
                result["world_feedback"] = {"window":feedback["window"],"commit_id":feedback["commit_id"],"visible_changes":changes}
        return result
