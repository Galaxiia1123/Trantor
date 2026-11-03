"""Derive a common opening only from passages the author explicitly marked for everyone."""
import re


PUBLIC_TITLE=r"(?:公开背景|共同背景|共同简报|全员共知|各方共同知道|public briefing|common knowledge)"
HEADING=re.compile(r"^\s*#{1,6}\s+("+PUBLIC_TITLE+r")\s*[:：]?\s*$",re.I)
INLINE=re.compile(r"^\s*"+PUBLIC_TITLE+r"\s*[:：]\s*(.+)$",re.I)


def separate_brief(scene,sources,*,prior=None):
    if prior and prior.provenance.kind=="user":
        scene["public_brief"]=prior.model_dump(mode="json")
        return
    excerpts=[]
    total=0
    for src in sources:
        if src.public_reference or src.simulation_ref:
            continue
        active=False
        fence=None
        for line in src.text.splitlines():
            marker=re.match(r"^\s*(`{3,}|~{3,})",line)
            if marker:
                if fence is None:fence=marker[1]
                elif marker[1].startswith(fence):fence=None
                continue
            if fence or line.lstrip().startswith(">"):
                continue
            if line.lstrip().startswith("#"):
                active=bool(HEADING.fullmatch(line));continue
            match=INLINE.fullmatch(line)
            text=match[1].strip() if match else line.strip() if active else ""
            if text and total<3000:
                part=text[:min(1200,3000-total)]
                excerpts.append({"source_id":src.id,"quote":part});total+=len(part)+1
    if excerpts:
        scene["public_brief"]={"text":"\n".join(q["quote"] for q in excerpts),"provenance":{"kind":"material",
            "source_ids":list(dict.fromkeys(q["source_id"] for q in excerpts)),"quotes":excerpts,
            "explanation":"采用原文明确标为全员共知的段落。"}}
        return
    private=any(not item.get("visibility",{}).get("public") for a in scene.get("entities",())
                for item in [*a.get("fields",()),*a.get("materials",())])
    if private or scene.get("scheduled_events") or any(not r.get("visibility",{}).get("public") for r in scene.get("rules",())):
        scene["public_brief"]={"text":"","provenance":{"kind":"missing","explanation":"材料没有明确标注全员共知的段落。各主体从自己的档案、可见规则和收件开始。"}}
    else:
        scene.pop("public_brief",None)


def runtime_scene(scene):
    from .schema import Scenario11
    if not isinstance(scene,Scenario11) or scene.public_brief is not None or not scene.sources:
        return scene
    data=scene.model_dump(mode="json")
    separate_brief(data,scene.sources)
    return Scenario11.model_validate(data) if data.get("public_brief") is not None else scene


def scope_generated_access_rules(scene):
    """Opening readers already get the structured ACL. Keep explanations of concealed access with the observer."""
    if scene.get("public_brief") is None:
        return
    for r in scene.get("rules",()):
        if r.get("provenance",{}).get("kind")=="user":
            continue
        if re.search(r"不会自动获知|不能自动获知|不会自动看到|只能由.{1,30}直接读取|只有.{1,35}能直接读取",r.get("text","")):
            r["visibility"]={"public":False,"readers":[]}


def check_public_numbers(scene):
    if scene.public_brief is None or not scene.public_brief.text:
        return
    actor_ids={a.id for a in scene.entities}
    fields=[(a,f) for a in scene.entities for f in a.fields if f.kind=="number"]
    sentences=re.split(r"[。！？!?；;\n]",scene.public_brief.text)
    for actor,field in fields:
        if field.visibility.public or actor_ids<={actor.id,*field.visibility.readers}:
            continue
        label=field.label or field.id
        shared_label=sum((f.label or f.id)==label for _,f in fields)>1
        values=[field.value,*[v.value for e in scene.scheduled_events for v in e.assignments if (v.entity_id,v.field_id)==(actor.id,field.id)]]
        amounts={str(v).removesuffix(".0") for v in values if v is not None}
        if not amounts:continue
        association=re.compile(re.escape(label)+r"(?:\s|为|是|仅|有|当前|现为|目前|起初|初始|变为|设为|定为|从|剩余|[:：=])*"+
                               r"(?:"+"|".join(re.escape(v) for v in amounts)+r")(?![\d.])\s*"+re.escape(field.unit or ""))
        if any(association.search(s) and (not shared_label or actor.name in s or actor.id in s) for s in sentences):
            raise ValueError(f"共同简报写出了私有字段“{actor.name} · {label}”的数值，请核对简报与该字段的知情范围")
