"""Reconcile unambiguous, explicitly named fields and directional statements with their source text."""
import re
import hashlib
from copy import deepcopy

from .schema import Scenario11, Source
from .briefing import separate_brief, scope_generated_access_rules
from .source_windows import apply_entry_tables, apply_meaning_tables


FIELD = re.compile(r"""(?:字段(?:名(?:为)?)?|(?:state\s+)?field(?:\s+named)?)\s*[“「『"'‘\x60]\s*([^”」』"'’\x60\n]{1,70}?)\s*[”」』"'’\x60]""", re.I)
SENTENCES = re.compile(r"[^。！？!?\n]+[。！？!?]?")


def _table_field_names(text):
    field_headers = {"字段","字段名","字段名称","状态字段","字段标识","field","field name","field id","state field"}
    value_headers = {"初始值","值","类型","单位","可见性","initial value","value","type","unit","visibility"}
    indices, pending, example = [], [], False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            example = bool(re.search(r"示例|举例|example",line,re.I))
            indices = pending = []
        if example or "|" not in line:
            indices = pending = []
            continue
        cells = [s.strip().strip("*\x60 ").strip() for s in re.split(r"(?<!\\)\|",line.strip("|"))]
        headers = [s.casefold() for s in cells]
        matches = [i for i,name in enumerate(headers) if name in field_headers]
        if matches and value_headers.intersection(headers):
            pending, indices = matches,[]
        elif pending:
            indices = pending if all(re.fullmatch(r":?-{2,}:?",s) for s in cells) else []
            pending = []
        elif indices:
            for i in indices:
                if i < len(cells) and 0 < len(cells[i]) <= 70:
                    yield cells[i]


def confirm_named_fields(payload):
    """Resolve only machine-generated missing-field questions after an explicit edit."""
    if payload["scenario"].get("format_version") != "1.1" or not payload.get("assistant",{}).get("revision_reason"):
        return
    defined = {v for a in payload["scenario"]["entities"] for f in a["fields"] if f["provenance"]["kind"]=="user"
               for v in (f.get("label"),f["id"]) if v}
    for record in payload["preparation_records"]:
        meaning_review=record.get("meaning_review",{})
        pending=meaning_review.get("unresolved",[])
        confirmed={item["question"] for item in pending for a in payload["scenario"]["entities"] if not item["actor"] or item["actor"] in (a["id"],a["name"])
                   for f in a["fields"] if item["field"] in (f["id"],f.get("label")) for m in (f.get("constraints") or {}).get("meanings",[])
                   if m["value"]==item["value"] and m["provenance"]["kind"]=="user"}
        if confirmed:
            meaning_review["unresolved"]=[i for i in pending if i["question"] not in confirmed]
            record["blocking_questions"]=[q for q in record.get("blocking_questions",[]) if q not in confirmed]
            payload["scenario"]["missing_information"]=[q for q in payload["scenario"]["missing_information"] if q not in confirmed]
        timing=record.get("entry_window_review",{})
        remaining=timing.get("unresolved",[])
        resolved_periods={item["question"] for item in remaining for a in payload["scenario"]["entities"]
            if not item["actor"] or item["actor"] in (a["id"],a["name"])
            for f in a["fields"] if item["field"] in (f["id"],f.get("label"))
            for rule in (f.get("constraints") or {}).get("entry_windows",[]) if rule["value"]==item["value"] and rule["provenance"]["kind"]=="user"}
        if resolved_periods:
            timing["unresolved"]=[i for i in remaining if i["question"] not in resolved_periods]
            record["blocking_questions"]=[q for q in record.get("blocking_questions",[]) if q not in resolved_periods]
            payload["scenario"]["missing_information"]=[q for q in payload["scenario"]["missing_information"] if q not in resolved_periods]
        doc_review=record.get("document_review",{})
        explicit={(m["document"]["title"],hashlib.sha256(m["text"].encode()).hexdigest()) for a in payload["scenario"]["entities"]
                  for m in a["materials"] if m.get("document") and m["provenance"]["kind"]=="user"}
        unresolved=doc_review.get("unresolved",[])
        fixed={d["question"] for d in unresolved if (d["title"],d["sha256"]) in explicit}
        if fixed:
            doc_review["unresolved"]=[d for d in unresolved if d["question"] not in fixed]
            record["blocking_questions"]=[q for q in record.get("blocking_questions",[]) if q not in fixed]
            payload["scenario"]["missing_information"]=[q for q in payload["scenario"]["missing_information"] if q not in fixed]
        check = record.get("source_reconciliation", {})
        missing = check.get("unresolved_fields", [])
        resolved = set(missing) & defined
        conflicts = set(check.get("conflicted_fields",[])) & defined
        if not resolved and not conflicts:
            continue
        questions = {f"材料明确列出了“{name}”，但草案缺少对应结算字段，请核对归属和初值。" for name in resolved}
        questions.update(f"材料对是否定义“{name}”有相互冲突的要求，请明确采用的设定。" for name in conflicts)
        record["blocking_questions"] = [q for q in record.get("blocking_questions",[]) if q not in questions]
        payload["scenario"]["missing_information"] = [q for q in payload["scenario"]["missing_information"] if q not in questions]
        check["unresolved_fields"] = [name for name in missing if name not in resolved]
        check["conflicted_fields"] = sorted(set(check.get("conflicted_fields",[])) - conflicts)
        check["user_resolved_fields"] = sorted(set(check.get("user_resolved_fields",[])) | resolved | conflicts)


def _named_fields(materials):
    named, excluded = set(), set()
    negative = re.compile(r"(?:不得|不应|不可|不要|未|不|禁止|无需)(?:自行|再|额外|擅自)?(?:创建|新增|设立|定义|设置|使用|启用|设)?(?:任何|新的|一个|该|此)?\s*$|(?:no|without|do not create|exclude)\s+(?:a\s+|the\s+)?$",re.I)
    for source in materials:
        named.update(_table_field_names(source["text"]))
        for sentence in SENTENCES.finditer(source["text"]):
            line = sentence.group()
            for match in FIELD.finditer(line):
                before, after = line[:match.start()], line[match.end():].lstrip()
                label = match.group(1).strip()
                if negative.search(before) or re.match(r"(?:不予|暂不|未被|不被)(?:定义|创建|启用|采用)",after):
                    excluded.add(label)
                elif not re.search(r"(?:例如|比如|举例|假如|假设以后|for example|e\.g\.)[^，,。]*$",before,re.I):
                    if re.match(r"(?:初始|初值|开局|起初|固定|为|是|当前|取|表示|可取|默认|[=：:]|starts?\b|is\b|equals?\b)",after,re.I) or re.search(r"(?:定义|设置|新增|登记|包含|维护|declares?|define[s]?)\s*(?:一个|a|the)?\s*$",before,re.I):
                        named.add(label)
    return named, excluded


def _aliases(entities):
    names = {a["id"]: {a["id"], a["name"]} for a in entities}
    # Unique suffixes accommodate common short mentions such as “维修社” after “芦湾维修社”.
    # The entire mention must stand next to a direction word, not merely appear somewhere in a clause.
    all_names = [a["name"] for a in entities]
    for a in entities:
        name = a["name"]
        if re.fullmatch(r"[\u3400-\u9fff]{4,}", name):
            for length in range(3, min(8,len(name))):
                suffix = name[-length:]
                if sum(n.endswith(suffix) for n in all_names) == 1:
                    names[a["id"]].add(suffix)
    return {name:key for key,options in names.items() for name in options if sum(name in v for v in names.values()) == 1}


def _sharing_rules(bundle):
    scene, record = bundle["scenario"], bundle["preparation_records"][-1]
    version=2 if any(m.get("document") for a in scene["entities"] for m in a["materials"]) else 1
    if record.get("sharing_review",{}).get("version") == version:
        return bundle
    sources = {s["id"]:s["text"] for s in scene["sources"] if not s.get("public_reference") and not s.get("simulation_ref")}
    defaults = []
    for actor in scene["entities"]:
        for m in actor["materials"]:
            if not m.get("protected_terms") and not (m.get("document") and m.get("sharing")):
                continue
            rule = m.get("sharing")
            provenance = rule.get("provenance",{}) if rule else {}
            if rule:
                if m["provenance"]["kind"]=="user":
                    continue
                if (provenance.get("kind") in {"material","user"} and provenance.get("quotes") and
                        all(q["quote"] in sources.get(q["source_id"],"") for q in provenance["quotes"])):
                    provenance["kind"]="material"
                    continue
            # Missing evidence cannot grant a future recipient access to a confidential literal.
            m["sharing"] = {"allowed_recipients":list(m["visibility"]["readers"]),
                            "provenance":{"kind":"ai_assumption","explanation":"未提供可核对的对外传播授权，按开局知情名单限制原文投递。"}}
            defaults.append({"actor":actor["id"],"terms":m["protected_terms"]})
    record["sharing_review"] = {"version":version,"restricted_to_initial_readers":defaults}
    bundle["scenario"] = Scenario11.model_validate(scene).model_dump(mode="json")
    return bundle


def _reader_named(reader, quote, actors):
    aliases=_aliases(actors)
    if any(key==reader and name in quote for name,key in aliases.items()):
        return True
    actor=next((a for a in actors if a["id"]==reader),None)
    if not actor or not re.fullmatch(r"[\u3400-\u9fff]{3,}",actor["name"]):
        return False
    short=actor["name"][-2:]
    if sum(a["name"].endswith(short) for a in actors)!=1:
        return False
    return re.search(r"(?:^|[，。；\n])\s*"+re.escape(short)+r"(?:有权|可以|可|已获准)(?:直接)?(?:查阅|读取|访问)",quote) is not None


def _resolve_read_copies(scene,record):
    if record.get("read_view_review"):
        return
    sources={s["id"]:s for s in scene["sources"] if not s.get("public_reference") and not s.get("simulation_ref")}
    named,_=_named_fields(sources.values())
    converted=[]
    for reader in scene["entities"]:
        remaining=[]
        for field in reader["fields"]:
            label=field.get("label") or field["id"]
            stem=re.sub(r"[（(](?:查阅|读取|可见|观察)[）)]$","",label)
            candidates=[(a,f) for a in scene["entities"] if a["id"]!=reader["id"] for f in a["fields"]
                        if stem in {a["name"]+(f.get("label") or f["id"]),a["name"]+"的"+(f.get("label") or f["id"])}]
            quotes=[q for q in field.get("provenance",{}).get("quotes",()) if q["source_id"] in sources
                    and q["quote"] in sources[q["source_id"]]["text"] and _reader_named(reader["id"],q["quote"],scene["entities"])
                    and re.search(r"(?:有权|可|获准)(?:直接)?(?:查阅|读取|访问)",q["quote"])]
            if len(candidates)!=1 or not quotes or field.get("provenance",{}).get("kind")=="user" or named.intersection({field["id"],label}):
                remaining.append(field)
                continue
            owner,original=candidates[0]
            visibility=original.setdefault("visibility",{})
            readers=visibility.setdefault("readers",[])
            if reader["id"] not in readers:readers.append(reader["id"])
            grants=visibility.setdefault("reader_evidence",[])
            if not any(g["reader_id"]==reader["id"] for g in grants):
                grants.append({"reader_id":reader["id"],"quote":quotes[0]})
            converted.append({"reader":reader["id"],"owner":owner["id"],"field":original["id"],"removed_copy":field["id"],"quote":quotes[0]})
        reader["fields"]=remaining
    if converted:
        record["read_view_review"]={"converted":converted}
        record.pop("visibility_review",None)


def _initial_access(bundle):
    scene, record = bundle["scenario"], bundle["preparation_records"][-1]
    if record.get("visibility_review",{}).get("version") == 1:
        return _sharing_rules(bundle)
    sources = {s["id"]:s for s in scene["sources"]}
    aliases = _aliases(scene["entities"])
    pending = []
    for actor in scene["entities"]:
        for obj in [*actor["fields"],*actor["materials"]]:
            visibility = obj["visibility"]
            if visibility["public"] or obj["provenance"]["kind"] == "user":
                continue
            approved = {actor["id"]}
            evidence = []
            for grant in visibility.get("reader_evidence",[]):
                q = grant["quote"]
                source = sources.get(q["source_id"])
                if not source or source.get("public_reference") or source.get("simulation_ref") or not q["quote"]:
                    continue
                body = source["text"]
                if q.get("page") is not None:
                    page = next((p for p in source.get("pages",[]) if p["number"]==q["page"]),None)
                    if not page:
                        continue
                    body = body[page["text_start"]:page["text_end"]]
                if q["quote"] not in body:
                    continue
                if not _reader_named(grant["reader_id"],q["quote"],scene["entities"]):
                    continue
                # Proof must establish an existing information right or prior delivery.
                # A future offer to tell the reader cannot confer knowledge at time zero.
                if not re.search(r"(?:已知|知晓|知情|共知|已.{0,8}(?:收悉|获知|收到|获准|披露|送达|共享)|有权(?:查阅|访问|读取)|可(?:见|查阅|读取|访问)|already|received|knows|may access|read access)",q["quote"],re.I):
                    continue
                if grant["reader_id"] in visibility["readers"]:
                    approved.add(grant["reader_id"])
                    evidence.append(grant)
            removed = [r for r in visibility["readers"] if r not in approved]
            if removed:
                pending.append({"owner":actor["id"],"item":obj.get("id") or obj["text"][:80],"readers":removed,
                                "reason":"未提供这些读者在开局时已经获知或有权查阅的原文"})
            visibility["readers"] = [r for r in visibility["readers"] if r in approved]
            visibility["reader_evidence"] = evidence
    record["visibility_review"] = {"version":1,"pending_grants":pending}
    return _sharing_rules(bundle)


DECLARED_DOCUMENT=re.compile(
    r"(?:原件|附件|单据|凭证|文件|document|attachment)\s*(?:的?标题|的?名称|名|title|name)?\s*(?:为|是|叫作|叫|[:：=])?\s*"
    r"[《“「『\"](?P<title>[^》”」』\"\n]{1,100})[》”」』\"](?:[，,。.;：:\s]|其){0,12}"
    r"(?:正文|原文|票面文字|body|contents?)\s*(?:为|是|如下|[:：=])?\s*[“「『\"](?P<body>[^”」』\"]{1,1200})[”」』\"]",re.I)


def _document_declarations(scene,record):
    """Rebuild only a named, verbatim original with an unambiguous initial owner and domain."""
    if record.get("document_review",{}).get("version")==1:
        return
    aliases=_aliases(scene["entities"])
    actors={a["id"]:a for a in scene["entities"]}
    unresolved,restored=[],[]
    for source in scene["sources"]:
        if source.get("public_reference") or source.get("simulation_ref"):
            continue
        for paragraph in re.split(r"\n\s*\n",source["text"]):
            if re.match(r"\s*(?:例如|示例|举例|example\b)",paragraph,re.I):
                continue
            matches=list(DECLARED_DOCUMENT.finditer(paragraph))
            for m in matches:
                title,body=m["title"].strip(),m["body"]
                existing=[(a,p) for a in actors.values() for p in a["materials"] if p.get("document") and p["document"]["title"]==title]
                if any(p["provenance"]["kind"]=="user" for a,p in existing):
                    continue
                digest=hashlib.sha256(body.encode()).hexdigest()
                sentences=[s.group(0) for s in SENTENCES.finditer(paragraph)]
                holders={}
                for alias,aid in aliases.items():
                    name=re.escape(alias)
                    pattern=rf"(?:开局|初始|最初|起初)(?:时|只有|仅有|仅由|由|仅|，|,|\s)*{name}(?:一方)?(?:持有|保管)|{name}(?:在开局时|最初|初始|开局时)(?:独自|单独)?(?:持有|保管)"
                    for sentence in sentences:
                        if re.search(pattern,sentence):
                            holders[aid]=sentence
                exact=[(a,p) for a,p in existing if p["text"]==body]
                if exact and (len(holders)!=1 or (len(existing)==1 and exact[0][0]["id"] in holders and
                              not exact[0][1]["visibility"]["public"] and not exact[0][1]["visibility"]["readers"])):
                    continue
                if len(holders)!=1 or len(matches)!=1 or len(scene["domains"])!=1 or len(existing)!=len(exact) or any(len(q)>1200 for q in holders.values()):
                    question=f"材料中有《{title}》的正文，请确认它的初始持有人、领域或采用的版本。"
                    unresolved.append({"title":title,"sha256":digest,"source":source["id"],"question":question})
                    continue
                owner,holder_quote=next(iter(holders.items()))
                permitted,denied={},set()
                for sentence in sentences:
                    if len(sentence)>1200 or not (title in sentence or re.search("原件|副本|此单据|该凭证|这份文件",sentence)):
                        continue
                    owner_named=any(alias in sentence for alias,aid in aliases.items() if aid==owner)
                    if not owner_named and not re.search("该原件|此原件|本文件|这份(?:单据|凭证|文件)",sentence):
                        continue
                    for alias,aid in aliases.items():
                        if aid==owner or not re.search(r"(?:寄给|寄送给|发送给|转交给?|交给|转寄给|送给|发给)\s*"+re.escape(alias),sentence):
                            continue
                        if re.search(r"不得|禁止|不能|不允许|未获准|无权",sentence):
                            denied.add(aid)
                        elif re.search(r"允许|授权|获准|可(?:向|以|随信|将|把|寄|转)",sentence):
                            permitted[aid]=sentence
                grants={a:q for a,q in permitted.items() if a not in denied}
                def provenance(quotes,reason):
                    return {"kind":"material","source_ids":[source["id"]],"explanation":reason,
                            "quotes":[{"source_id":source["id"],"quote":q} for q in dict.fromkeys(quotes)]}
                key="doc_"+hashlib.sha256((source["id"]+"\0"+title+"\0"+body).encode()).hexdigest()[:14]
                if exact:
                    key=exact[0][1]["document"]["id"]
                    for a,p in exact:a["materials"].remove(p)
                material={"domain":scene["domains"][0],"text":body,"document":{"id":key,"title":title},
                          "visibility":{"public":False,"readers":[]},"protected_terms":[],
                          "provenance":provenance([body,holder_quote],"逐字采用材料列出的原件正文及初始持有人。")}
                if grants:
                    material["sharing"]={"allowed_recipients":sorted(grants),"provenance":provenance(grants.values(),"采用材料中明确授予的寄送范围。")}
                actors[owner]["materials"].append(material)
                public=scene["background"]
                quoted=r"[,，；;]\s*(?:其)?(?:正文|原文)(?:是|为|如下|[:：])?\s*[“「『\"]"+re.escape(body)+r"[”」』\"]"
                scoped=re.sub(quoted,"。",public)
                if len(body)>=8:
                    scoped=scoped.replace(body,"〔正文随原件保管〕")
                scene["background"]=scoped
                restored.append({"id":key,"source":source["id"],"owner":owner,"removed_public_copy":scoped!=public})
    questions=[item["question"] for item in unresolved]
    record["document_review"]={"version":1,"restored":restored,"unresolved":unresolved}
    if questions:
        record["blocking_questions"]=list(dict.fromkeys([*record.get("blocking_questions",[]),*questions]))
        scene["missing_information"]=list(dict.fromkeys([*scene["missing_information"],*questions]))


def reconcile(result):
    bundle = deepcopy(result)
    scene, record = bundle["scenario"], bundle["preparation_records"][-1]
    _resolve_read_copies(scene,record)
    from .source_capacity import apply_capacity_tables
    apply_capacity_tables(scene,record)
    _document_declarations(scene,record)
    if not record.get("common_brief_review"):
        if scene.get("public_brief") is None:
            separate_brief(scene,[Source.model_validate(s) for s in scene["sources"]])
        scope_generated_access_rules(scene)
        record["common_brief_review"]=True
    apply_entry_tables(scene,record)
    apply_meaning_tables(scene,record)
    if record.get("source_reconciliation",{}).get("version") == 1:
        bundle["questions"]=record.get("blocking_questions",[])
        return _initial_access(bundle)
    materials = [s for s in scene["sources"] if not s.get("public_reference") and not s.get("simulation_ref")]
    named, excluded = _named_fields(materials)
    conflict = named & excluded
    suggestions, restored = [], []
    for actor in scene["entities"]:
        fields = []
        for field in actor["fields"]:
            label = field.get("label") or field["id"]
            field_names = {label,field["id"]}
            if (not field_names.intersection(excluded) and (not named or field_names.intersection(named))) or field["provenance"]["kind"] == "user":
                fields.append(field)
                continue
            # The seed names its settlement fields explicitly. Preserve other facts in the owner's dossier.
            # A proposed additional field remains available for explicit adoption.
            if field_names.intersection(excluded):
                continue
            value = field["value"]
            prose = label + "尚未明确" if value is None else f"{label}为{value}{field.get('unit') or ''}"
            added = not any(m["text"] == prose for m in actor["materials"])
            suggestions.append({"actor_id":actor["id"],"field":field,"dossier_text":prose if added else None})
            if added:
                actor["materials"].append({"domain":field["domain"],"text":prose,"visibility":{"public":False,"readers":[]},
                                           "provenance":field["provenance"]})
        actor["fields"] = fields
    aliases = _aliases(scene["entities"])
    alts = "|".join(re.escape(a) for a in sorted(aliases,key=len,reverse=True))
    direct = re.compile(r"(?P<a>" + alts + r")(?:也|还)?(?:可以|可|能够|有权|获准|应当|必须)(?:直接)?向(?P<b>" + alts + r")")
    arrow = re.compile(r"(?P<a>" + alts + r")\s*(?:→|->)\s*(?P<b>" + alts + r")\s*[：:]\s*\S")
    pairs = {(e["source"],e["target"]) for e in scene["influences"]}
    for source in materials:
        for match in SENTENCES.finditer(source["text"]):
            quote = match.group().strip()
            if not quote or len(quote) > 900:
                continue
            if re.search(r"(?:并非|不代表|假设以后|假如|反例|例如|比如)", quote):
                continue
            for pattern in (direct,arrow):
                for link in pattern.finditer(quote):
                    a,b = aliases[link["a"]],aliases[link["b"]]
                    if a == b or (a,b) in pairs:
                        continue
                    edge = {"source":a,"target":b,"description":quote,"provenance":{"kind":"material",
                            "source_ids":[source["id"]],"quotes":[{"source_id":source["id"],"quote":quote}],
                            "explanation":"沿用原文中明示方向的关系描述"}}
                    scene["influences"].append(edge)
                    pairs.add((a,b)); restored.append({"source":a,"target":b,"source_id":source["id"]})
    # Never complete a draft by inventing the value, owner or unit of an omitted explicit field.
    actual = {v for a in scene["entities"] for f in a["fields"] for v in (f.get("label"),f["id"]) if v}
    absent = sorted(named - actual)
    for label in absent:
        question = f"材料明确列出了“{label}”，但草案缺少对应结算字段，请核对归属和初值。"
        record.setdefault("blocking_questions", []).append(question)
        scene["missing_information"].append(question)
    for label in sorted(conflict):
        question = f"材料对是否定义“{label}”有相互冲突的要求，请明确采用的设定。"
        record.setdefault("blocking_questions", []).append(question)
        scene["missing_information"].append(question)
    bundle["questions"] = record.get("blocking_questions", [])
    record["source_reconciliation"] = {"version":1, "named_fields":sorted(named),"restored_relations":restored,
                                       "suggested_fields":suggestions,"unresolved_fields":absent,
                                       "excluded_fields":sorted(excluded),"conflicted_fields":sorted(conflict)}
    return _initial_access(bundle)
