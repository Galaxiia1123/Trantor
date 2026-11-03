"""Validate a window identically during live settlement and offline replay."""
from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy

from .schema import Decision, FieldReview, RunConfig11, Scenario11, Settlement, message_scope, validate_outbox, valid_value
from .perception import Perception
from .pacing import ReviewPlan, revised_deadlines


def scheduled_reviews(state, receipts):
    rows={}
    for event in receipts:
        for effect in event["data"]["assignments"]:
            owner,field=effect["entity_id"],effect["field_id"]
            rows[owner,field]=FieldReview(entity_id=owner,field_id=field,before=state[owner][field],after=effect["value"],
                reason="按本窗外部事件的预定值结算："+event["data"]["text"],event_ids=[event["id"]])
    return rows


def complete_scheduled_reviews(state, proposal, receipts):
    forced=scheduled_reviews(state,receipts)
    if not forced:
        return proposal
    ordinary=[]
    included=set()
    for review in proposal.reviews:
        key=review.entity_id,review.field_id
        required=forced.get(key)
        if required is None:
            ordinary.append(review)
        else:
            if (key in included or not _same(review.before,required.before) or not _same(review.after,required.after) or
                review.action_ids or review.influence_refs or review.event_ids not in ([],required.event_ids)):
                raise ValueError(f"字段 {key[0]}.{key[1]} 已有明确的外部事件设定，请省略该条审查并由引擎按预定值结算")
            included.add(key)
    return proposal.model_copy(update={"reviews":[*ordinary,*forced.values()]})


def settled_state(scenario, state, proposal, action_ids, action_owners=None, receipts=()):
    definitions = {(a.id, f.id): f for a in scenario.entities for f in a.fields}
    reviews = {(r.entity_id, r.field_id): r for r in proposal.reviews}
    if len(reviews) != len(proposal.reviews) or reviews.keys() != definitions.keys():
        raise ValueError("世界必须审查全部且仅已定义字段，每字段一次")
    next_state = deepcopy(state)
    edges = {(e.source,e.target) for e in scenario.influences}
    limits = scenario.delivery_limits() if isinstance(scenario,Scenario11) else {}
    forced=scheduled_reviews(state,receipts)
    event_ids={event["id"] for event in receipts}
    changes = []
    for key, review in reviews.items():
        actor_id, field_id = key
        before = state[actor_id][field_id]
        if type(review.before) is not type(before) and not (type(review.before) in (int, float) and type(before) in (int, float)):
            raise ValueError("结算前值类型不匹配")
        if review.before != before or not valid_value(definitions[key].kind, review.after):
            raise ValueError("结算前值或目标类型不匹配")
        field = definitions[key]
        if not field.permits(review.after) or (field.constraints and field.constraints.fixed and review.after != before):
            raise ValueError(f"字段 {actor_id}.{field_id} 的提案超出用户定义的取值范围或修改权限")
        if isinstance(review.after,str) and limits:
            readers = set(state) if field.visibility.public else {actor_id,*field.visibility.readers}
            if any(not readers <= set(allowed) for allowed in message_scope(review.after,limits).values()):
                raise ValueError(f"字段 {actor_id}.{field_id} 的新值含有超出该字段读者范围的保密短语")
        if not set(review.action_ids) <= action_ids:
            raise ValueError("结算引用了本窗口之外的行动")
        if not set(review.event_ids)<=event_ids:
            raise ValueError("结算引用了未在本窗口发生的外部事件")
        if key in forced and review!=forced[key]:
            raise ValueError(f"字段 {actor_id}.{field_id} 与本窗明确的外部设定不符")
        for ref in review.influence_refs:
            location = f"直接影响引用 {actor_id}.{field_id}（{ref.source} → {ref.target}）"
            if (ref.source,ref.target) not in edges or ref.target != actor_id:
                allowed = sorted(a for a,b in edges if b == actor_id)
                choices = "允许的源主体只有 " + "、".join(allowed) if allowed else "该字段所属主体没有登记入边"
                raise ValueError(f"{location} 不匹配开局的有向边。{choices}。保持原值或没有可核对的直接作用时请返回 influence_refs=[]")
            missing = sorted(set(ref.action_ids) - set(review.action_ids))
            if missing:
                raise ValueError(f"{location} 的 {', '.join(missing)} 未列入同一字段的 action_ids。若确有影响，同时在该字段列出这些行动；无直接作用则移除这条 influence_refs")
            if not action_owners or any(action_owners.get(eid) != ref.source for eid in ref.action_ids):
                raise ValueError(f"{location} 只可引用源主体 {ref.source} 在本窗口亲自发出的行动")
        if before != review.after:
            if not review.action_ids and key not in forced:
                raise ValueError("状态变更必须关联窗口行动")
            next_state[actor_id][field_id] = review.after
            changes.append(review.model_dump(mode="json"))
    if any(not set(item.action_ids) <= action_ids for item in proposal.consequences):
        raise ValueError("客观后果引用不存在的行动")
    if any(not set(item.event_ids)<=event_ids for item in proposal.consequences):
        raise ValueError("客观后果引用了未在本窗口发生的外部事件")
    return next_state, changes


def _same(left, right):
    # JSON booleans and numbers must not compare equal during ledger validation.
    return json.dumps(left, sort_keys=True, ensure_ascii=False) == json.dumps(right, sort_keys=True, ensure_ascii=False)


def verify_commits(scenario, config, events):
    """Validate committed rounds and state transitions; abandoned evidence remains readable."""
    state = scenario.initial_state()
    actors = {actor.id: actor for actor in scenario.entities}
    permitted_fields = {actor.id: {field.id: field for field in actor.fields} for actor in actors.values()}
    perception = Perception(scenario, scenario.depths(), config) if isinstance(config, RunConfig11) else None
    last_feedback = None
    last_window = 0
    last_commit_index = 0
    discarded = set()
    for index, e in enumerate(events):
        if e["kind"] == "window_committed":
            last_commit_index = index
        elif e["kind"] == "run_recovered":
            candidates = {prior["id"] for prior in events[last_commit_index + 1:index]
                          if prior["kind"] in {"observation","expression","decision_deferred","external_event","message_delivered","model_input","model_output","model_validation_error","model_failure"}}
            dropped = set(e["data"]["discarded_event_ids"])
            if not dropped <= candidates or discarded & dropped:
                raise ValueError(f"恢复记录 {e['id']} 引用了当前未提交尝试之外的事件")
            discarded.update(dropped)
    schedule = Counter()
    observations = Counter()
    expected_messages = Counter()
    deliveries = Counter()
    action_ids = set()
    action_owners = {}
    inboxes = {owner: [] for owner in actors}
    observed_per_round = Counter()
    acted_per_round = Counter()
    total_actions = total_messages = total_deferred = 0
    plans, visible_at_round, deadlines = {}, {}, {}
    receipts=[]
    expected_actions = len(actors) * config.rounds_per_window
    for event in events:
        if event["id"] in discarded:
            continue
        kind, data = event["kind"], event["data"]
        if kind not in {"expression","decision_deferred","external_event","observation","message_delivered","window_committed"}:
            continue
        if event["window"] != last_window + 1:
            raise ValueError(f"事件 {event['id']} 的窗口与提交边界不一致")
        scheduled=perception.calendar.get(event["window"],()) if perception else ()
        if kind=="external_event":
            if observations or len(receipts)>=len(scheduled) or not _same(data,scheduled[len(receipts)].model_dump(mode="json")):
                raise ValueError("外部事件与场景设定、顺序或生效窗口不一致")
            receipts.append(event)
            continue
        if len(receipts)!=len(scheduled):
            raise ValueError("主体观察或窗口提交之前缺少预定的外部事件")
        if kind in {"expression","decision_deferred"}:
            owner = data["actor_id"]
            if owner not in actors or not 1 <= data["round"] <= config.rounds_per_window:
                raise ValueError("表达的主体或轮次不合法")
            if observed_per_round[data["round"]] != len(actors):
                raise ValueError("决定之前并非所有主体都已得到本轮观察")
            schedule[owner, data["round"]] += 1
            acted_per_round[data["round"]] += 1
            visible=visible_at_round.get(owner)
            if not visible or visible["round"]!=data["round"] or visible["window"]!=event["window"]:
                raise ValueError("该主体的决定与观察轮次不一致")
            plan=plans.get(owner)
            reason=plan.wake_reason(visible) if plan else None
            if kind=="decision_deferred":
                if not plan or reason or data!={"actor_id":owner,"round":data["round"],"plan_record":plan.record,"review_at":plan.review_at}:
                    raise ValueError("等待记录与主体此前安排、当前收件或可见处境不符")
                continue
            resumed={"reason":reason,"plan_record":plan.record,"review_at":plan.review_at} if plan and reason else None
            if (plan and not reason) or data.get("resumed_plan")!=resumed:
                raise ValueError("重新判断的记录与原安排及当时可见信息不符")
            if perception:
                original = Decision.model_validate_json(data["raw"])
                validate_outbox(original,perception.delivery_limits)
                if original.text != data["text"] or [m.model_dump() for m in original.messages] != data["messages"]:
                    raise ValueError("主体表达或发件人与当时的原文不符")
                deadlines[owner]=revised_deadlines(deadlines.get(owner,{}),original.matters)
                new_plan=ReviewPlan.from_decision(original,event["id"],visible,config.max_review_interval,deadlines[owner].values())
                if data.get("next_review_window")!=(new_plan.review_at if new_plan else None):
                    raise ValueError("记录的复看时间与主体原始表达或窗口上限不符")
                if new_plan: plans[owner]=new_plan
                else: plans.pop(owner,None)
            action_ids.add(event["id"])
            action_owners[event["id"]] = owner
            for message in data["messages"]:
                for recipient in set(message["recipients"]):
                    if recipient not in actors:
                        raise ValueError("表达包含不存在的收件人")
                    expected_messages[event["id"], owner, recipient, message["text"]] += 1
        elif kind == "message_delivered":
            key = data["action_id"], data["from"], data["to"], data["text"]
            if deliveries[key] >= expected_messages[key]:
                raise ValueError(f"投递 {event['id']} 与该表达的收件人或内容不符")
            if perception and data.get("sharing",{}) != message_scope(data["text"],perception.delivery_limits):
                raise ValueError(f"投递 {event['id']} 的通信权限记录不符")
            deliveries[key] += 1
            inboxes[data["to"]].append(data)
        elif kind == "observation":
            viewer, visible = data["actor_id"], data["visible"]
            if viewer not in actors or visible["self"]["id"] != viewer:
                raise ValueError("主体观察归属不符")
            turn = visible["round"]
            visible_at_round[viewer]=visible
            if perception:
                expected = perception.observe(viewer,state,inboxes[viewer],event["window"],turn,last_feedback,receipts)
                if not _same(visible, expected):
                    raise ValueError(f"观察 {event['id']} 与该主体当时的完整可见范围不符")
            if (visible["window"] != event["window"] or not 1 <= turn <= config.rounds_per_window
                    or (turn > 1 and acted_per_round[turn - 1] != len(actors))):
                raise ValueError("主体观察越过了轮次屏障")
            observations[viewer, visible["round"]] += 1
            observed_per_round[turn] += 1
            for owner, values in visible["visible_state"].items():
                for name, description in values.items():
                    field = permitted_fields.get(owner, {}).get(name)
                    if not field or not field.visibility.allows(owner, viewer) or not _same(description["value"],state[owner][name]):
                        raise ValueError(f"观察 {event['id']} 的字段范围或数值不符")
            for material in visible["visible_materials"]:
                owner = material["owner_id"]
                if owner not in actors or not any(m.domain == material["domain"] and m.text == material["text"] and
                                                  m.visibility.allows(owner, viewer) for m in actors[owner].materials):
                    raise ValueError(f"观察 {event['id']} 包含未授权材料")
            if visible["messages"] != inboxes[viewer]:
                raise ValueError(f"观察 {event['id']} 与该主体在屏障后实际收到的消息不符")
            inboxes[viewer] = []
        else:
            if (event["window"] > config.windows or len(schedule) != expected_actions or any(n != 1 for n in schedule.values())
                    or observations != schedule or deliveries != expected_messages
                    or data.get("deferred_reviews",0)!=sum(schedule.values())-len(action_ids)):
                raise ValueError(f"窗口 {event['window']} 的主体轮次或投递不完整")
            proposal = Settlement.model_validate({k: data[k] for k in ("reviews","consequences","suggested_fields")})
            next_state, changes = settled_state(scenario,state,proposal,action_ids,action_owners,receipts)
            recorded = [FieldReview.model_validate(c).model_dump(mode="json") for c in data["changes"]]
            if not _same(next_state, data["state"]) or not _same(changes, recorded):
                raise ValueError(f"提交 {event['id']} 与字段审查重放结果不符")
            state = next_state
            last_window = event["window"]
            last_feedback = {"window":last_window,"commit_id":event["id"],"changes":changes}
            total_actions += len(action_ids)
            total_deferred += sum(schedule.values())-len(action_ids)
            total_messages += sum(deliveries.values())
            schedule.clear()
            observations.clear()
            observed_per_round.clear()
            acted_per_round.clear()
            expected_messages.clear()
            deliveries.clear()
            action_ids.clear()
            action_owners.clear()
            receipts=[]
    outputs = verify_references(events)
    complete_pending = (len(schedule) == expected_actions and all(n == 1 for n in schedule.values())
                        and observations == schedule and deliveries == expected_messages)
    return {"committed_windows": last_window, "verified_expressions": total_actions, "verified_deferred":total_deferred,"verified_deliveries": total_messages,
            "pending_actions_complete":complete_pending,
            **outputs,
            "discarded_records": len(discarded), "uncommitted_expressions": len(action_ids), "state": state}


def actor_evidence(event, actor_id):
    data = event["data"]
    if event["kind"] in {"expression","decision_deferred","observation"}:
        return data.get("actor_id") == actor_id
    return event["kind"] == "message_delivered" and data.get("to") == actor_id


def verify_references(events):
    seen = {}
    reports = answers = 0
    for event in events:
        data = event["data"]
        if event["kind"]=="expression" and data.get("model_output_id"):
            response = seen.get(data["model_output_id"])
            if (not response or response["kind"]!="model_output" or response["window"]!=event["window"]
                    or response["data"].get("actor_id")!=data["actor_id"] or response["data"].get("purpose")!="agent"
                    or ("text" in response["data"] and response["data"]["text"]!=data["raw"])):
                raise ValueError("表达与所属主体当时的模型响应不符")
        if event["kind"] == "report_created":
            reports += 1
            for section in data["report"]["sections"]:
                for item in [section, *section.get("claims", [])]:
                    ids = item["evidence_ids"]
                    if not ids or any(eid not in seen for eid in ids):
                        raise ValueError(f"报告 {event['id']} 引用了不存在或晚于报告的记录")
        elif event["kind"] == "analysis_answer":
            answers += 1
            supplied = {item["id"] for item in data["evidence"]}
            if not supplied <= seen.keys() or not data["evidence_ids"] or not set(data["evidence_ids"]) <= supplied:
                raise ValueError("存档回答引用了未提供的记录")
            actor_id = data.get("actor_id")
            for eid in supplied:
                record = seen[eid]
                if record["window"] > data["cutoff_window"] or (actor_id and not actor_evidence(record,actor_id)):
                    raise ValueError("存档回答的证据超出窗口或主体权限")
        seen[event["id"]] = event
    return {"verified_reports":reports,"verified_answers":answers}
