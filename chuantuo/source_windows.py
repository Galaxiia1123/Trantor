"""Read explicit Markdown entry periods for existing categorical fields."""
import re


COLUMNS={"主体":"actor","所属主体":"actor","actor":"actor","字段":"field","字段名":"field","field":"field",
         "目标值":"value","候选值":"value","value":"value","最早转入窗口":"first","first window":"first",
         "最晚转入窗口":"last","last window":"last"}


def _entries(text):
    cols={}
    header_ready=False
    for raw in text.splitlines():
        line=raw.strip()
        if "|" not in line:
            cols={};header_ready=False
            continue
        parts=[p.strip().strip("`") for p in re.split(r"(?<!\\)\|",line.strip("|"))]
        labels={COLUMNS[p.lower()]:i for i,p in enumerate(parts) if p.lower() in COLUMNS}
        if {"field","value","first","last"}<=labels.keys():
            cols=labels;header_ready=False
            continue
        if not cols:
            continue
        if not header_ready:
            header_ready=all(re.fullmatch(r":?-{2,}:?",p) for p in parts)
            if not header_ready:cols={}
            continue
        if max(cols.values())>=len(parts):
            continue
        row={key:parts[i] for key,i in cols.items()}
        first=row["first"]
        last=row["last"]
        minimum=int(first) if first.isdecimal() and len(first)<10 else 0
        maximum=(int(last) if last.isdecimal() and len(last)<10 else -1) if last.lower() not in {"","—","-","不限","无上限","open"} else None
        yield row.get("actor",""),row["field"],row["value"],minimum,maximum,line


def apply_entry_tables(scene,record):
    if "entry_window_review" in record:
        return
    adopted=[];unresolved=[]
    for source in scene["sources"]:
        if source.get("public_reference") or source.get("simulation_ref"):
            continue
        for actor,field,value,first,last,line in _entries(source["text"]):
            found=[(a,f) for a in scene["entities"] if not actor or actor in (a["id"],a["name"])
                   for f in a["fields"] if field in (f["id"],f.get("label")) and value in (f.get("constraints") or {}).get("choices",())]
            valid=len(found)==1 and first>=1 and (last is None or first<=last) and len(line)<=1200
            if valid:
                a,f=found[0]
                periods=f["constraints"].setdefault("entry_windows",[])
                current=next((r for r in periods if r["value"]==value),None)
                if current and current.get("provenance",{}).get("kind")=="user":
                    continue
                if current and (current.get("first",1),current.get("last"))!=(first,last):
                    valid=False
                elif not current:
                    periods.append({"value":value,"first":first,"last":last,"provenance":{"kind":"material","source_ids":[source["id"]],
                                    "explanation":"原文表格明确规定了该值的转入窗口。","quotes":[{"source_id":source["id"],"quote":line}]}})
                    adopted.append({"actor":a["id"],"field":f["id"],"value":value,"first":first,"last":last,"source":source["id"]})
            if not valid:
                unresolved.append({"actor":actor,"field":field,"value":value,"question":f"请核对“{field}”中“{value}”的所属主体和转入期限，原表与现有字段或其他期限未能对齐。"})
    record["entry_window_review"]={"adopted":adopted,"unresolved":unresolved}
    if unresolved:
        questions=[r["question"] for r in unresolved]
        record["blocking_questions"]=list(dict.fromkeys([*record.get("blocking_questions",[]),*questions]))
        scene["missing_information"]=list(dict.fromkeys([*scene["missing_information"],*questions]))
