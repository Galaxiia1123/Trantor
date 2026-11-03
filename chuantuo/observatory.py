"""Answer field questions from committed records, including unknown and not-yet-observed windows."""
import json
import html
import re
from typing import Literal

from pydantic import Field

from .models import protect_credentials
from .schema import Identifier, Record, Value, valid_value


class FieldWatch(Record):
    actor_id: Identifier
    field_id: Identifier
    title: str = Field(default="", max_length=160)
    operator: Literal["track","equals","at_least","at_most","known"] = "track"
    criterion: Literal["ever","always","at_end"] = "ever"
    target: Value = None
    deadline: int | None = Field(default=None, ge=0)

    def resolve(self, scene, configs=()):
        actor = next((a for a in scene.entities if a.id==self.actor_id),None)
        field = next((f for f in actor.fields if f.id==self.field_id),None) if actor else None
        if not field:
            raise ValueError("只能关注场景里已经定义的字段")
        target=self.target
        if field.kind=="number" and isinstance(target,str):
            try:
                target=json.loads(target)
            except ValueError:
                raise ValueError("请输入有效的目标数值") from None
        if self.operator in {"track","known"}:
            if self.target is not None:
                raise ValueError("该观察方式无需目标值")
        elif target is None or not valid_value(field.kind,target):
            raise ValueError("目标值需要与所选字段的类型一致")
        if self.operator in {"at_least","at_most"} and field.kind!="number":
            raise ValueError("上下限比较只适用于数值字段")
        if self.operator=="track" and (self.deadline is not None or self.criterion!="ever"):
            raise ValueError("追踪字段时无需填写达成条件或期限")
        protect_credentials([self.title,self.target],configs)
        return actor, field, target

    def matches(self, value, target):
        if self.operator=="known":
            return value is not None
        if value is None or self.operator=="track":
            return None
        if self.operator=="at_least":
            return value >= target
        if self.operator=="at_most":
            return value <= target
        return value == target


def observe_field(simulation, question, through=None, *, details=True):
    actor, field, target = question.resolve(simulation._scenario,simulation.config.catalog.values())
    with simulation._lock:
        latest=simulation.window
        selected=latest if through is None else through
        if not 0<=selected<=latest:
            raise ValueError("请从已经提交的窗口中选择观察终点")
        horizon=min(selected,question.deadline) if question.deadline is not None else selected
        value=field.value
        segments=[]
        first_hit=None
        first_miss=None
        unknown_windows=0
        changes=0
        for window in range(horizon+1):
            commit=simulation.journal.settlement(window) if window else None
            previous=value
            value=commit["data"]["state"][actor.id][field.id] if commit else field.value
            record=commit["id"] if commit else "e000001"
            hit=question.matches(value,target)
            if value is None:
                unknown_windows+=1
            if hit and first_hit is None:
                first_hit={"window":window,"record":record}
            if hit is False and first_miss is None:
                first_miss={"window":window,"record":record}
            if window and value!=previous:
                changes+=1
            if details:
                if window==0 or value!=previous:
                    review=next((r for r in commit["data"]["changes"] if r["entity_id"]==actor.id and r["field_id"]==field.id),None) if commit else None
                    segments.append({"start":window,"end":window,"value":value,"numeric_text":str(value) if field.kind=="number" and value is not None else None,"record":record,"matches":hit,
                                     "reason":review["reason"] if review else "", "actions":review["action_ids"] if review else []})
                else:
                    segments[-1]["end"]=window
        terminal=simulation.status in {"completed","stopped"} and selected==latest
        deadline_reached=question.deadline is not None and selected>=question.deadline
        early=terminal and question.deadline is not None and selected<question.deadline
        closed=deadline_reached or terminal
        if question.operator=="track":
            status="tracking"
        elif question.criterion=="always":
            if first_miss: status="violated"
            elif early: status="ended_early"
            elif unknown_windows: status="undetermined" if closed else "pending"
            else: status="maintained" if closed else "holding"
        elif question.criterion=="at_end":
            if early: status="ended_early"
            elif not closed: status="pending_end"
            elif hit is None: status="undetermined_end"
            else: status="met_at_end" if hit else "missed_at_end"
        elif first_hit is not None:
            status="observed"
        elif early:
            status="ended_early"
        elif not closed:
            status="pending"
        elif unknown_windows and question.operator!="known":
            status="undetermined"
        else:
            status="not_observed"
        return {"question":question.model_dump(mode="json"),"actor":actor.name,"label":field.label or field.id,
                "kind":field.kind,"unit":field.unit,"latest":latest,"through":horizon,"record":record,
                "value":value,"numeric_text":str(value) if field.kind=="number" and value is not None else None,
                "changes":changes,"first_hit":first_hit,"first_miss":first_miss,"matches":hit,
                "unknown_windows":unknown_windows,"status":status,"segments":segments}


def ongoing_matters(simulation, through, actor_id=None, status=None, offset=0, limit=30):
    """Reconstruct private notebook episodes as they stood at the selected commit."""
    with simulation._lock:
        end=simulation.window if through is None else through
        if not 0<=end<=simulation.window or (actor_id and actor_id not in simulation.depths):
            raise ValueError("请选择已提交窗口和场景中的主体")
        titles={a.id:a.name for a in simulation._scenario.entities}
        active,episodes={},[]
        for window in range(1,end+1):
            actions,_=simulation.journal.window_rows(window,0,actor=actor_id,kind="expression")
            for action in actions:
                if action["id"] in simulation._discarded:
                    continue
                data=action["data"]
                for change in json.loads(data["raw"]).get("matters",[]):
                    key=(data["actor_id"],change["key"])
                    note=active.get(key)
                    step={"window":window,"round":data["round"],"record":action["id"]}
                    if change.get("status","open")=="closed":
                        if note:
                            note["status"]="closed"
                            note["steps"].append({**step,"kind":"closed","text":change.get("text","")})
                            del active[key]
                    else:
                        text=change["text"]
                        if note is None:
                            note={"actor_id":key[0],"actor":titles[key[0]],"key":key[1],"status":"open","steps":[]}
                            episodes.append(note)
                            active[key]=note
                        due=change.get("due_window",note.get("due_window"))
                        if note["steps"] and note["text"]==text and note.get("due_window")==due:
                            continue
                        note["steps"].append({**step,"kind":"updated" if note["steps"] else "opened","text":text,"due_window":due})
                        note["text"]=text
                        note["due_window"]=due
        chosen=[n for n in episodes if not status or n["status"]==status]
        chosen.sort(key=lambda n:int(n["steps"][-1]["record"][1:]),reverse=True)
        return {"through":end,"total":len(chosen),"open":len(active),"closed":len(episodes)-len(active),
                "items":chosen[offset:offset+limit],"next":min(offset+limit,len(chosen))}


def observation_notes(simulation, watches, through=None):
    if not watches:
        raise ValueError("先固定一个观察问题，再导出观察笔记")
    def plain(text):
        escaped=html.escape(" ".join(str(text).splitlines()),quote=False)
        return re.sub(r"([\\`*_[\]()#!|~])",r"\\\1",escaped)
    def phrase(value,unit):
        if value is None: return "未知"
        if isinstance(value,bool): return "是" if value else "否"
        return plain(value)+plain(unit or "")
    end=simulation.window if through is None else through
    rows=[observe_field(simulation,FieldWatch.model_validate(spec),end,details=False) for spec in watches.values()]
    lines=["# "+plain(simulation._scenario.title)+" · 观察笔记","",f"运行 `{simulation.run_id}`，选取至第 {end} 窗的提交。","","以下问题均依据本机推演记录计算。"]
    sources={}
    for r in rows:
        q=r["question"]; target=q["target"]; unit=r["unit"]
        title=q["title"] or r["actor"]+" · "+r["label"]
        lines+=["","## "+plain(title),"",plain(r["actor"])+"的"+plain(r["label"])+"。"]
        if q["operator"]!="track":
            goal={"equals":"等于","at_least":"至少达到","at_most":"不超过","known":"出现已知值"}[q["operator"]]
            lines.append("观察条件为"+goal+(phrase(target,unit) if target is not None else "")+("，期限为第 "+str(q["deadline"])+" 窗" if q["deadline"] is not None else "")+"。")
        hit=r["first_hit"]; latest=r["record"]
        def cite(event,window):
            sources.setdefault(event,set()).add((window,r["actor"],r["label"]))
            return f"[{event}](#event-{event})"
        lines.append(f"第 {r['through']} 窗终值为"+phrase(r["value"],unit)+f"，期间有 {r['changes']} 次字段变化。"+cite(latest,r["through"]))
        if q["criterion"]=="always" and r["first_miss"]:
            miss=r["first_miss"]
            lines.append(f"第 {miss['window']} 窗首次记录到不满足条件。"+cite(miss["record"],miss["window"]))
        elif hit and q["criterion"]=="ever":
            lines.append(f"首次记录达成在第 {hit['window']} 窗。"+cite(hit["record"],hit["window"]))
        elif q["operator"]!="track":
            phrases={"undetermined":"存在未知时点，无法确定整个观察期的结果。","pending":"观察尚未结束，记录仍不足以得出结论。",
                     "ended_early":"运行提前结束，记录未覆盖整个期限。","not_observed":"已观察的期限内未出现目标结果。",
                     "holding":"截至目前每个时点均满足条件，仍在观察。","maintained":"包括开局在内，整个观察期始终满足条件。",
                     "pending_end":"指定终点尚未到达。","undetermined_end":"观察终点的值未知。","met_at_end":"观察终点满足条件。","missed_at_end":"观察终点不满足条件。"}
            lines.append(phrases[r["status"]])
    lines+=["","## 记录出处"]
    for eid,refs in sorted(sources.items(),key=lambda item:int(item[0][1:])):
        lines+=["",f'<a id="event-{eid}"></a>',"",f"- `{eid}`　"+"、".join(f"第 {w} 窗的{plain(a)} · {plain(f)}" for w,a,f in sorted(refs))]
    return "\n".join(lines)+"\n"
