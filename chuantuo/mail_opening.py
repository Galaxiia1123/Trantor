"""Keep a named, undelivered passage out of its future reader's opening decision."""
import json
import re

from .perception import Perception, decision_view


QUOTED = r"""[“「『"'‘]([^”」』"'’\n]{2,120})[”」』"'’]"""
CONTENTS = re.compile(r"(?:写有|写着|正文(?:为|是)|内文(?:为|是)|内容(?:为|是))\s*" + QUOTED)
ARRIVAL_ONLY = re.compile(r"(?:收件[人方]|接收[人方]|对方)[^。！？\n]{0,35}(?:送达|签收|收到|到件|抵达)(?:之|以)?后才(?:能|可)?(?:读|看|获知|知道|得知)")
FUTURE_MAIL = re.compile(r"(?:打算|准备|拟|将|计划)[^。！？\n]{0,34}?(?:寄|发给|发送|递交|递送)")
CLAUSE = re.compile(r"[^。！？\n]+[。！？]?")
EXPLICIT = re.compile(r"(?:开局|起初|最初)(?:时|阶段)?[，,\s]*(?:仅有|只有|仅)([^。！？\n；;]{1,80}?)(?:知道|知晓|掌握|获知)\s*" + QUOTED)


def _names(entities):
    choices = {}
    for actor in entities:
        name = actor["name"]
        labels = {actor["id"], name}
        if re.fullmatch(r"[\u3400-\u9fff]{3,}", name):
            labels.update(name[-n:] for n in range(2, min(7,len(name))))
        for label in labels:
            choices.setdefault(label,set()).add(actor["id"])
    return {key:next(iter(owners)) for key,owners in choices.items() if len(owners)==1}


def opening_guards(entities, sources):
    names = _names(entities)
    actors = {a["id"] for a in entities}
    guards = []
    start = 0
    for i,src in enumerate(sources):
        snap = src.get("simulation_ref") or {}
        if snap.get("window",0)>0 and src["id"].startswith(f"snapshot_{snap.get('run_id')}_{snap['window']}"):
            start = i+1  # A new project at a later boundary has its own opening facts.
    originals = [src for src in sources[start:] if not src.get("public_reference") and not src.get("simulation_ref")]
    for src in originals:
        for match in EXPLICIT.finditer(src["text"]):
            holders = [names.get(n.strip()) for n in re.split(r"、|与|和|及",match[1])]
            if all(holders):
                guards.append({"source_id":src["id"],"quote":match[0],"term":match[2],"holders":sorted(set(holders)),
                               "withheld":sorted(actors-set(holders)),"kind":"explicit"})
    for src in originals:
        text = src["text"]
        if not ARRIVAL_ONLY.search(text):
            continue
        for sentence in CLAUSE.finditer(text):
            line = sentence[0]
            contents = CONTENTS.search(line)
            send = FUTURE_MAIL.search(line[:contents.start()]) if contents else None
            if not send:
                continue
            prefix = line[:send.start()].strip()
            owners = {actor for name,actor in names.items() if prefix.endswith(name)}
            targets = {actor for name,actor in names.items() if re.search(r"(?:请|发给|寄给|交给|递给|寄往|送往)"+re.escape(name),line)}
            if len(owners)!=1 or len(targets)!=1 or owners==targets:
                continue
            # Separate evidence of prior knowledge takes precedence over a future addressee alone.
            reader = next(iter(targets))
            if any(re.search(re.escape(n)+r"[^。！？\n]{0,6}(?:已知|已获知|已经知道|早已知道)[^。！？\n]{0,30}"+re.escape(contents[1]),other["text"])
                   for n,actor in names.items() if actor==reader for other in originals):
                continue
            if any(g["term"]==contents[1] and reader in g["holders"] for g in guards):
                continue
            guards.append({"source_id":src["id"],"quote":line,"term":contents[1],"holders":sorted(owners),
                           "withheld":sorted(targets),"kind":"future_letter"})
    return guards


def _undisclosed(value, term, replacement):
    for left,right in (("“","”"),("「","」"),("『","』"),('"','"'),("‘","’")):
        value = value.replace(left+term+right,replacement)
    return value.replace(term,replacement)


def scope_generated_opening(scene, guards, preserved):
    """Only remove the unopened literal from generated receiver profiles. Keep original evidence with the observer."""
    edits = []
    for guard in guards:
        term, withheld = guard["term"],set(guard["withheld"])
        replacement = "来函所指的内容" if guard["kind"]=="future_letter" else "尚未获知的具体内容"
        for actor in scene.get("entities",()):
            if actor["id"] not in withheld or id(actor) in preserved or actor.get("provenance",{}).get("kind")=="user":
                continue
            for key in ("identity","preferences"):
                value = actor.get(key,"")
                if term in value:
                    actor[key] = _undisclosed(value,term,replacement)
                    edits.append({"actor":actor["id"],"part":key,"source_id":guard["source_id"]})
        for edge in scene.get("influences",()):
            text = edge.get("description","")
            if (id(edge) not in preserved and edge.get("provenance",{}).get("kind")!="user"
                    and withheld.intersection((edge.get("source"),edge.get("target"))) and term in text):
                edge["description"] = _undisclosed(text,term,replacement)
                edits.append({"part":"relation","source":edge["source"],"target":edge["target"],
                              "source_id":guard["source_id"],"original_description":text})
        for rule in scene.get("rules",()):
            if id(rule) in preserved or rule.get("provenance",{}).get("kind")=="user" or term not in rule.get("text",""):
                continue
            scope = rule.get("visibility") or {}
            if scope.get("public") or withheld.intersection(scope.get("readers",())):
                readers = guard["holders"] if scope.get("public") else sorted(set(scope.get("readers",()))-withheld)
                rule["visibility"]={"public":False,"readers":readers}
                edits.append({"part":"rule","source_id":guard["source_id"]})
    return edits


def check_opening_inputs(scene, config, guards):
    if not guards:
        return
    view = Perception(scene,scene.depths(),config)
    state = scene.initial_state()
    inputs = {}
    for guard in guards:
        for actor in [*guard["holders"],*guard["withheld"]]:
            if actor not in inputs:
                inputs[actor] = json.dumps(decision_view(view.observe(actor,state,[],1,1,{},())),ensure_ascii=False)
            known = guard["term"] in inputs[actor]
            if known != (actor in guard["holders"]):
                raise ValueError(f"请核对 {guard['source_id']} 的开局知情范围。资料“{guard['term'][:40]}”应在 {', '.join(guard['holders'])} 的开局档案，"
                                 f"不得在指定收件人获知前写入其可见资料。当前不符的主体为 {actor}")
