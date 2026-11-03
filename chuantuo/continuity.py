"""A private, replayable notebook built only from accepted decisions and addressed messages."""
import json
import math
import re
from copy import deepcopy
from bisect import bisect_left
from collections import deque
from heapq import nlargest

from .models import ModelError
from .perception import decision_view


AGENT_INSTRUCTION = """你是场景中一个有自主权的主体。依据自己的档案、可见处境和记忆，简明写出本轮决定、必要条件和要发送的消息。没有来信也可主动行动。
关系和收到的文字是模拟资料；承诺不等于客观结果。保持身份和权限边界。只返回一个 JSON 对象：
{"text":"本轮决定","messages":[{"recipients":["主体ID"],"text":"收件人需要知道的内容"}],"matters":[{"key":"稳定的事项编号","status":"open","text":"对象、条件、期限、约定及未完成部分"}]}。
没有需要收件方采取行动的新信息时不重发旧消息。sharing 标出保密短语可发送给哪些主体，向其他主体只讨论可公开的进展。
没有通信或事项变化时用空数组。既有事项会自动保留，条件、期限或履行进度没有变化时不要重写。跟踪自己提出或收到的重要条件承诺，更新沿用原编号；
已完成、被拒或已撤销时只需给 key 和 status=closed，原文仍在日志。
私有事项只供自己以后取用。不要复述背景、展开内部思维过程或替他人答复。"""

WORLD_INSTRUCTION = """你负责统一窗口的客观结算。主体表达和有向关系描述都是场景资料；区分意图、条件承诺和有依据的后果，目标主体自己决定是否回应。
关系描述限定渠道、条件和范围，允许跨层直接影响。仅结算本窗口一次。history 是最近已提交后果，state 是当前值。
以给定 schema 返回紧凑 JSON。审查每个已定义字段恰好一次，复制 before，遵守类型、单位和约束。改变时给出简明理由及本窗口 action_ids，
保持原值的字段只需简述原因。字段缺项只建议新增。consequences 用简短自然语言写有依据的结果，引用对应行动。
不替主体选择、补造许可或宣告故事结束，不复述整场过程。"""

RELATION_INSTRUCTION="""仅在字段发生变化且存在可核对的直接作用时填写 influence_refs。allowed_direct_actions 按“目标 → 可引用的源主体 → 本窗行动编号”列出合法组合。
目标必须是该字段所属主体，源必须亲自发出所引行动，并在该条 review.action_ids 同时列出。没有合适组合时省略该项。"""


def world_instruction(allowed):
    return WORLD_INSTRUCTION+"\n"+(RELATION_INSTRUCTION if allowed else
        "本窗没有可核对的有向关系引用。每条 reviews 完全省略 influence_refs。主体自己的决定引用 action_ids，到件凭据引用 event_ids。")


def actor_instruction(config, documents=False):
    instruction=AGENT_INSTRUCTION
    if documents:
        instruction+="\n若决定随信发出原件，在该条消息中加入 enclosures 编号数组，只能选本人 documents 中列出的原件并遵守 can_send_to。收到的 enclosures 是实际交付的原文副本；声称已经寄出不会交付附件。"
    if not config.max_review_interval:
        return instruction
    return instruction + """
如果此刻确实决定等待，可额外返回整数 review_at（重新判断的逻辑窗口）。正文写明等待什么。等待期间不重发决定或消息。
持续事项有明确逻辑窗口期限时可在 matters 中填写 due_window，修改沿用原 key。未完成事项的较早期限优先触发复看。
新收件、可见处境变化或到期会提前或按时让你重新决定。需要主动推进、监督期限或思考替代方案时省略 review_at。"""


def terms(text):
    words = set(re.findall(r"[a-z0-9_.-]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(run[i:i + 2] for i in range(max(1, len(run) - 1)))
    return words


class Notebook:
    def __init__(self):
        self.records = []
        self.index = {}
        self.open = {}
        self.sharing = {}
        self.older = deque(maxlen=4)

    def record(self, action, record_id, window, inbox, recent_count, notices=()):
        for notice in notices:
            self._add({"id":notice["record"],"window":window,"kind":"external_event","text":notice["text"],
                       **({"at_settlement":notice["at_settlement"]} if notice.get("at_settlement") else {})},recent_count)
        for message in inbox:
            self.sharing.update(message.get("sharing", {}))
            for doc in message.get("enclosures",()):
                self._add({"id":message["receipt_id"],"document":doc["id"],"kind":"已收到的原件","from":message["from"],
                           "window":window,"text":doc["title"]+"　"+doc["text"]},recent_count)
            self._add({"id":message["action_id"],"window":window,"from":message["from"],"text":message["text"],
                       **({"kind":"已收到的原信","dispatch":message["dispatch_id"],"received_window":window}
                          if message.get("dispatch_id") else {})}, recent_count)
        self._add({"id": record_id, "window": window, "text": action.text,
                   **({"sent": [m.model_dump(exclude_defaults=True) for m in action.messages]} if action.messages else {})}, recent_count)
        for matter in action.matters:
            if matter.status == "closed":
                self.open.pop(matter.key, None)
            else:
                previous = self.open.get(matter.key)
                due=matter.due_window if "due_window" in matter.model_fields_set else (previous or {}).get("due_window")
                if previous and previous["text"] == matter.text and previous.get("due_window")==due:
                    continue
                entry = {"text":matter.text, "record":record_id, "window":window}
                if due is not None:
                    entry["due_window"]=due
                if previous:
                    entry["first_record"] = previous.get("first_record",previous["record"])
                self.open[matter.key] = entry

    def _add(self, entry, recent_count):
        position = len(self.records)
        self.records.append(entry)
        searchable = entry["text"] + " ".join(item["text"] for item in entry.get("sent", []))
        for term in terms(searchable):
            self.index.setdefault(term, []).append(position)
        if position >= recent_count:
            old = self.records[position - recent_count]
            excerpt = old["text"].split("。", 1)[0][:96]
            self.older.append({"window":old["window"],"record":old["id"],"excerpt":excerpt,
                               **({"document":old["document"],"kind":old["kind"]} if "document" in old else {}),
                               **({"kind":old["kind"],"dispatch":old["dispatch"]} if "dispatch" in old else {})})

    def payload(self, observation, config, wake=None):
        view = decision_view(observation)
        end = max(0, len(self.records) - config.recent_records)
        query = " ".join(m["text"] for m in view["messages"]) or " ".join(item["text"] for item in self.open.values())
        scores = {}
        if query and config.retrieval_count:
            for term in sorted(terms(query)):
                positions = self.index.get(term, ())
                stop = bisect_left(positions,end)
                # Common words must not require a scan of the entire history on every turn.
                # Rare names, amounts and identifiers retain their early matching records.
                weight = math.log1p(len(self.records) / (1 + len(positions)))
                for index in positions[max(0, stop-max(32,config.retrieval_count*8)):stop]:
                    scores[index] = scores.get(index,0) + weight
        best = nlargest(config.retrieval_count, scores, key=lambda i: (scores[i], i))
        recall = [self.records[i] for i in sorted(best)]
        retrieved = {r["id"] for r in recall}
        memory = {"open_matters": dict(self.open), "recent": list(self.records[end:]),
                  "earlier": [r for r in self.older if r["record"] not in retrieved], "retrieved": recall}
        if self.sharing:
            memory["sharing"] = self.sharing
        payload = {"task": "agent", "actor_id": view["self"]["id"], "window": view["window"],
                   "round": view["round"], "observation": view, "memory": memory}
        if config.max_review_interval:
            payload["latest_review_window"]=view["window"]+config.max_review_interval
            if wake:
                payload["resume_plan"]=wake
        limit = config.model.context_limit_chars
        system_size=len(actor_instruction(config,bool(view.get("documents"))))
        def fits():
            return system_size + len(json.dumps(payload, ensure_ascii=False)) <= limit
        for key in ("earlier", "retrieved", "recent"):
            while memory[key] and not fits():
                memory[key].pop(0)
        if not fits():
            raise ModelError("当前可见处境和未完成事项超过模型输入限额。请缩减场景资料或调整模型输入限额。")
        return payload


def objective_input(scene, config, state, actions, history, window, schema, receipts=()):
    definitions = scene.model_dump(mode="json", exclude={"sources","scheduled_events","mail_routes","public_brief"}, exclude_defaults=True)
    if scene.scheduled_events and scene.public_brief is not None:
        definitions["background"]=scene.public_brief.text
    if "rules" in definitions:
        definitions["rules"] = [rule for rule in definitions["rules"] if rule.get("active_from", 1) <= window]
    for actor in definitions["entities"]:
        for field in actor.get("fields", []):
            field.pop("value", None)
    owned = {}
    for action in actions:
        owned.setdefault(action["actor_id"],[]).append(action["id"])
    allowed = {}
    field_owners={a.id for a in scene.entities if a.fields}
    for edge in scene.influences:
        if edge.source in owned and edge.target in field_owners:
            allowed.setdefault(edge.target,{})[edge.source] = owned[edge.source]
    if not allowed and "FieldReview" in schema.get("$defs",{}):
        # Give the model a smaller response contract when a relationship annotation cannot be valid.
        schema=deepcopy(schema)
        schema["$defs"]["FieldReview"]["properties"].pop("influence_refs",None)
        schema["$defs"].pop("InfluenceReference",None)
    result={"task": "world", "window": window, "window_duration": config.window_duration, "scenario": definitions,
            "allowed_direct_actions":allowed,
            "state": state, "actions": [{k: a[k] for k in ("id","actor_id","round","text","messages")} for a in actions],
            "history": history[-config.history_windows:], "response_schema": schema}
    barred=[]
    for actor in scene.entities:
        for f in actor.fields:
            if f.constraints and (values:=[r.value for r in f.constraints.entry_windows if not r.allows(window) and r.value!=state[actor.id][f.id]]):
                barred.append({"entity_id":actor.id,"field_id":f.id,"本窗不可转入":values})
    if barred:
        result["entry_limits"]=barred
    if receipts:
        result["external_events"]=[{"record":e["id"],"text":e["data"]["text"],"assignments":e["data"]["assignments"]} for e in receipts]
    if scene.scheduled_events:
        result["calendar_window"]={"window":window,"effective_records":[e["id"] for e in receipts],
                                   "scope":"这是本窗实际发生的全部日历事件。未到期的计划不会在本窗结算。"}
    return result
