"""Read explicit Markdown entry periods for existing categorical fields."""
import re


COLUMNS={"主体":"actor","所属主体":"actor","actor":"actor","字段":"field","字段名":"field","field":"field",
         "目标值":"value","候选值":"value","value":"value","最早转入窗口":"first","first window":"first",
         "最晚转入窗口":"last","last window":"last","含义":"meaning","判定口径":"meaning","成立条件":"meaning","meaning":"meaning"}


def _table_rows(text,required,columns=COLUMNS):
    cols={}
    header_ready=False
    fence=None
    example_depth=None
    for raw in text.splitlines():
        line=raw.strip()
        if marker:=re.match(r"^(`{3,}|~{3,})",line):
            if fence is None:fence=marker[1]
            elif marker[1].startswith(fence):fence=None
            cols={};continue
        if not fence and (heading:=re.match(r"^(#{1,6})\s+(.+)",line)):
            depth=len(heading[1])
            if example_depth is None or depth<=example_depth:
                example_depth=depth if re.search(r"示例|举例|example",heading[2],re.I) else None
        if fence or example_depth or line.startswith(">") or "|" not in line:
            cols={};header_ready=False
            continue
        parts=[p.strip().strip("`") for p in re.split(r"(?<!\\)\|",line.strip("|"))]
        labels={columns[p.lower()]:i for i,p in enumerate(parts) if p.lower() in columns}
        if required<=labels.keys():
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
        yield {key:parts[i] for key,i in cols.items()},line


def _entries(text):
    for row,line in _table_rows(text,{"field","value","first","last"}):
        first=row["first"]
        last=row["last"]
        minimum=int(first) if first.isdecimal() and len(first)<10 else 0
        maximum=(int(last) if last.isdecimal() and len(last)<10 else -1) if last.lower() not in {"","—","-","不限","无上限","open"} else None
        yield row.get("actor",""),row["field"],row["value"],minimum,maximum,line


def apply_meaning_tables(scene,record):
    if "meaning_review" in record:return
    unresolved=[]
    for source in scene["sources"]:
        if source.get("public_reference") or source.get("simulation_ref"):continue
        for row,line in _table_rows(source["text"],{"field","value","meaning"}):
            owner,field,value,text=row.get("actor",""),row["field"],row["value"],row["meaning"]
            found=[f for a in scene["entities"] if not owner or owner in (a["id"],a["name"]) for f in a["fields"]
                   if field in (f["id"],f.get("label")) and value in (f.get("constraints") or {}).get("choices",())]
            if len(found)==1 and 0<len(text)<=500 and len(line)<=1200:
                meanings=found[0]["constraints"].setdefault("meanings",[])
                current=next((m for m in meanings if m["value"]==value),None)
                if current and current.get("provenance",{}).get("kind")=="user":continue
                if not current or current["text"]==text or any(q.get("source_id")==source["id"] and q.get("quote") and
                       (text in q["quote"] or len(q["quote"])>=8 and q["quote"] in text)
                       for q in current.get("provenance",{}).get("quotes",())):
                    if current:meanings.remove(current)
                    meanings.append({"value":value,"text":text,"provenance":{"kind":"material","source_ids":[source["id"]],
                        "quotes":[{"source_id":source["id"],"quote":line}],"explanation":"采用原文表格对该候选值的定义。"}})
                    continue
            q=f"请核对“{field}”的“{value}”在本场景中如何判定，两处定义或字段归属尚未对齐。"
            unresolved.append({"actor":owner,"field":field,"value":value,"question":q})
    record["meaning_review"]={"unresolved":unresolved}
    if unresolved:
        questions=[r["question"] for r in unresolved]
        record["blocking_questions"]=list(dict.fromkeys([*record.get("blocking_questions",[]),*questions]))
        scene["missing_information"]=list(dict.fromkeys([*scene["missing_information"],*questions]))


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
