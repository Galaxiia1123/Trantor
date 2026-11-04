"""Recognize optional, explicit capacity tables without manufacturing state fields."""
import hashlib
import math
import re

from .source_windows import _table_rows


COLUMNS={"共享上限":"name","共享约束":"name","capacity":"name","容量字段":"limit","容量":"limit","limit":"limit",
         "占用字段":"members","占用项":"members","members":"members"}


def apply_capacity_tables(scene,record):
    if "capacity_review" in record:
        return
    references={}
    for actor in scene["entities"]:
        for field in actor.get("fields",()):
            for a in {actor["id"],actor["name"]}:
                for f in {field["id"],field.get("label") or field["id"]}:
                    references.setdefault(a+"."+f,[]).append({"entity_id":actor["id"],"field_id":field["id"]})
    missing=[]
    declared={}
    for source in scene["sources"]:
        if source.get("public_reference") or source.get("simulation_ref"):
            continue
        for row,quote in _table_rows(source["text"],{"name","limit","members"},COLUMNS):
            limit=row["limit"]
            if re.fullmatch(r"\d+(?:\.\d+)?",limit) and len(limit)<=100:
                limit=float(limit) if "." in limit else int(limit)
                if isinstance(limit,float) and not math.isfinite(limit):
                    limit=None
            else:
                found=references.get(limit,[])
                limit=found[0] if len(found)==1 else None
            member_names=[s.strip() for s in re.split(r"、|,|，",row["members"])]
            members=[references[n][0] for n in member_names if len(references.get(n,()))==1]
            if limit is None or len(members)!=len(member_names) or len(members)<2 or not row["name"]:
                missing.append("共享上限“"+row["name"]+"”的容量或占用字段尚未唯一对应，请核对主体.字段的名称。")
                continue
            definition={"limit":limit,"members":members}
            if row["name"] in declared and declared[row["name"]]!=definition:
                missing.append("共享上限“"+row["name"]+"”在不同表行有冲突，请明确采用的容量和占用项。")
                continue
            declared[row["name"]]=definition
            pools=scene.setdefault("capacity_pools",[])
            current=next((p for p in pools if p["name"]==row["name"]),None)
            if current and current.get("provenance",{}).get("kind")=="user":
                continue
            if not current:
                current={"id":"cap_"+hashlib.sha256((source["id"]+row["name"]).encode()).hexdigest()[:12],"name":row["name"]}
                pools.append(current)
            current.update(definition,provenance={"kind":"material","source_ids":[source["id"]],
                "quotes":[{"source_id":source["id"],"quote":quote}],"explanation":"按文稿明确列出的共同容量约束。"})
    if not declared and not missing:
        return
    record["capacity_review"]={"defined":list(declared),"unresolved":missing}
    for issue in missing:
        if issue not in record.setdefault("blocking_questions",[]):record["blocking_questions"].append(issue)
        if issue not in scene.setdefault("missing_information",[]):scene["missing_information"].append(issue)
