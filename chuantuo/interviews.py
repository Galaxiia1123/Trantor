"""Evidence for a retrospective question at one accepted decision."""
from __future__ import annotations

from .engine import encode


def decision_evidence(simulation, actor_id, anchor_id):
    journal = simulation.journal
    try:
        action = journal.event(anchor_id)
    except FileNotFoundError:
        raise ValueError("这次运行没有指定的决定记录") from None
    if (action["kind"] not in {"expression", "decision_deferred"} or action["data"].get("actor_id") != actor_id
            or simulation.event_status(action) != "committed"):
        raise ValueError("请选择这个主体在已结算窗口中的决定")
    window, turn = action["window"], action["data"]["round"]
    context = journal.decision_context(actor_id, window, turn, simulation._discarded)
    if anchor_id != (context.get("action_id") or context.get("deferred_id")):
        raise ValueError("这条记录不是该轮最终采用的决定")
    deferred = action["kind"] == "decision_deferred"
    source_id = context["observation_id"] if deferred else context["input_id"]
    decision = {k: v for k, v in action["data"].items() if k not in {"raw", "model_output_id"}}
    pieces = [(anchor_id, action["kind"], "这一轮的等待安排" if deferred else "这一轮的决定和发信", decision)]
    view = context["observation"]
    labels = {"messages": "本轮实际收件", "documents": "当时持有的文书", "state": "可见处境",
              "external_events": "本轮收到的外部通知", "visible_materials": "可见资料", "rules": "当时可见的规则"}
    # Include empty inboxes and document lists. An empty list can answer a receipt question.
    kind = "observation" if deferred else "model_input"
    for key, value in sorted(view.items(), key=lambda item: item[0] not in labels):
        pieces.append((source_id, kind, labels.get(key, key), value))
    for key in ("memory", "coverage", "resumed_plan"):
        if key in context:
            pieces.append((source_id, kind, {"memory":"当时实际带入的记忆","coverage":"记忆覆盖范围","resumed_plan":"恢复判断的缘由"}[key], context[key]))
    return {"anchor_record":anchor_id, "context_record":source_id, "cutoff_window":window, "cutoff_round":turn,
            **({"decision_output":context["output_id"]} if not deferred else {})}, pieces


def select_decision_evidence(pieces, point, budget, terms):
    fragments = []
    for source_id, kind, section, value in pieces:
        text = value if isinstance(value, str) else encode(value)
        for start in range(0, max(1, len(text)), 1400):
            excerpt = text[start:start+1400]
            hit = sum(1 for term in terms if term in (section + excerpt).lower())
            item = {"id":source_id, "window":point["cutoff_window"], "kind":kind,
                    "locator":{"section":section, "start":start}, "excerpt":excerpt,
                    "excerpt_truncated":len(text)>len(excerpt)}
            fragments.append((source_id == point["anchor_record"] and start == 0, hit, item))
    ranked = sorted(fragments, key=lambda row:row[:2], reverse=True)
    first_input = next(i for i, row in enumerate(ranked) if row[2]["id"] == point["context_record"])
    ranked.insert(1, ranked.pop(first_input))
    selected, left = [], budget
    for _, _, item in ranked:
        if left < 150:
            break
        # Leave space for at least one piece from the other record when the limit is tight.
        limit = min(left, max(240, budget//2)) if not selected else left
        while len(encode(item)) + 2 > limit and len(item["excerpt"]) > 12:
            item = {**item, "excerpt":item["excerpt"][:len(item["excerpt"])//2], "excerpt_truncated":True}
        size = len(encode(item)) + 2
        if size <= limit:
            selected.append(item)
            left -= size
    if {item["id"] for item in selected} != {point["anchor_record"], point["context_record"]}:
        raise ValueError("字符限额放不下这次决定及其所见，请缩短问题或提高模型的上下文限额")
    return {"evidence":selected, "eligible_total":len(fragments), "matched_total":sum(hit>0 for _,hit,_ in fragments),
            "selection":f"取自这一轮的原始记录，提供了 {len(selected)} / {len(fragments)} 个片段。未提供的内容不能用于回答。"}
