"""A two-route field brief assembled from committed evidence at the reader's chosen cutoff."""
from .contrast import field_contrast
from .observatory import note_text
from .story import field_reason


def _field_entry(sim, window, actor, field):
    detail = field_reason(sim, window, actor, field, limit=4)
    evidence = []
    for row in detail["evidence"]:
        entry = {key: row[key] for key in (
            "id", "kind", "actor", "actor_id", "round", "sender", "recipient", "window", "observation"
        ) if key in row}
        entry.update(text=row["text"][:650], excerpt=len(row["text"]) > 650)
        evidence.append(entry)
    value = detail["after"]
    if value is None:
        shown = "未知"
    elif isinstance(value, bool):
        shown = "是" if value else "否"
    else:
        shown = str(value) + (detail["unit"] or "")
    return {"value": value, "shown": shown, "reason": detail["reason"], "record": detail["record"],
            "evidence": evidence, "total_evidence": detail["total"]}


def paired_brief(left, right, actor, field, through=None):
    focus = field_contrast(left, right, actor, field, through)
    split, end = focus["after"], focus["through"]
    if (right.experiment or {}).get("parent_id") == left.run_id:
        labels = ("原路线", "新路线")
    elif (left.experiment or {}).get("parent_id") == right.run_id:
        labels = ("新路线", "原路线")
    else:
        labels = ("路线 A", "路线 B")
    sides = []
    for label, sim in zip(labels, (left, right)):
        marker = sim.experiment or {}
        default = "沿用相同条件重新运行" if marker.get("kind") == "repeat" else "沿用原场景"
        sides.append({"run": sim.run_id, "label": label, "title": sim._scenario.title,
                      "condition": marker.get("condition") or default, "record": "e000001"})
    points = {split: "分岔起点"}
    if focus["first"]:
        points[focus["first"]["window"]] = "首个值不同的窗末"
    points[end] = points[end] + " · 观察终点" if end in points else "观察终点"
    field_points = [{"window": window, "label": label, "sides": [
        _field_entry(sim, window, actor, field) for sim in (left, right)
    ]} for window, label in sorted(points.items())]
    a, b = field_points[-1]["sides"]
    if a["value"] is None or b["value"] is None:
        summary = f"观察到{_when(end)}，至少一条路线的“{focus['field']}”仍未知。"
    elif a["value"] == b["value"]:
        summary = f"到{_when(end)}，两条路线的“{focus['field']}”同为“{a['shown']}”。"
    else:
        summary = f"到{_when(end)}，{labels[0]}的“{focus['field']}”为“{a['shown']}”，{labels[1]}为“{b['shown']}”。"
    options = [{"actor_id": a.id, "actor": a.name, "field_id": f.id, "label": f.label or f.id}
               for a in left._scenario.entities for f in a.fields]
    return {"actor": focus["actor"], "actor_id": actor, "field": focus["field"], "field_id": field,
            "options": options, "split": split, "through": end, "sides": sides, "points": field_points,
            "counts": focus["counts"], "summary": summary,
            "scope": "这份简报比较两次具体运行。结算理由与所引行动来自各路线的原记录，仅凭一对结果无法判断发生频率或单一原因。"}


def _when(window):
    return f"第 {window} 窗末" if window else "开局"


def brief_markdown(brief):
    esc=note_text
    lines=["# "+esc(brief["actor"])+" · "+esc(brief["field"])+"对照简报","",esc(brief["summary"]),"",
           f"观察范围为{_when(brief['split'])}至{_when(brief['through'])}。",""]
    for side in brief["sides"]:
        lines.extend([f"- {side['label']} · {esc(side['title'])} · {esc(side['condition'])}（`{side['run']}/{side['record']}`）"])
    counts=brief["counts"]
    lines.extend(["",f"分岔后有 {counts['equal']} 窗相同、{counts['different']} 窗不同、{counts['unknown']} 窗至少一侧未知。"])
    for point in brief["points"]:
        lines.extend(["",f"## {_when(point['window'])} · {point['label']}"])
        for side,row in zip(brief["sides"],point["sides"]):
            run=side["run"]
            lines.extend(["",f"### {side['label']} · {esc(row['shown'])}","",f"{esc(row['reason'])}（`{run}/{row['record']}`）"])
            for item in row["evidence"]:
                speaker=item.get("actor") or item.get("sender") or "外部记录"
                lines.append(f"- {esc(speaker)} · {esc(item['text'])}{'…（节选）' if item['excerpt'] else ''}（`{run}/{item['id']}`）")
            if row["total_evidence"]>len(row["evidence"]):
                lines.append(f"本项共引 {row['total_evidence']} 条记录，此处列出前 {len(row['evidence'])} 条。")
    return "\n".join([*lines,"",brief["scope"],""])
