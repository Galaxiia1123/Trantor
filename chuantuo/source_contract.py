"""Reconcile unambiguous, explicitly named fields and directional statements with their source text."""
import re
from copy import deepcopy

from .schema import Scenario11


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
                    if re.match(r"(?:初始|固定|为|是|当前|取|表示|可取|默认|[=：:]|starts?\b|is\b|equals?\b)",after,re.I) or re.search(r"(?:定义|设置|新增|登记|包含|维护|declares?|define[s]?)\s*(?:一个|a|the)?\s*$",before,re.I):
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
    if record.get("sharing_review",{}).get("version") == 1:
        return bundle
    sources = {s["id"]:s["text"] for s in scene["sources"]}
    defaults = []
    for actor in scene["entities"]:
        for m in actor["materials"]:
            if not m.get("protected_terms"):
                continue
            rule = m.get("sharing")
            provenance = rule.get("provenance",{}) if rule else {}
            if rule and (m["provenance"]["kind"]=="user" or (
                provenance.get("kind")=="material" and provenance.get("quotes") and
                all(q["quote"] in sources.get(q["source_id"],"") for q in provenance["quotes"]))):
                continue
            # Missing evidence cannot grant a future recipient access to a confidential literal.
            m["sharing"] = {"allowed_recipients":list(m["visibility"]["readers"]),
                            "provenance":{"kind":"ai_assumption","explanation":"未提供可核对的对外传播授权，按开局知情名单限制原文投递。"}}
            defaults.append({"actor":actor["id"],"terms":m["protected_terms"]})
    record["sharing_review"] = {"version":1,"restricted_to_initial_readers":defaults}
    bundle["scenario"] = Scenario11.model_validate(scene).model_dump(mode="json")
    return bundle


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
                if not source or not q["quote"]:
                    continue
                body = source["text"]
                if q.get("page") is not None:
                    page = next((p for p in source.get("pages",[]) if p["number"]==q["page"]),None)
                    if not page:
                        continue
                    body = body[page["text_start"]:page["text_end"]]
                if q["quote"] not in body:
                    continue
                if not any(key == grant["reader_id"] and name in q["quote"] for name,key in aliases.items()):
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


def reconcile(result):
    bundle = deepcopy(result)
    scene, record = bundle["scenario"], bundle["preparation_records"][-1]
    if record.get("source_reconciliation",{}).get("version") == 1:
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
