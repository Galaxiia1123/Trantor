"""Track explicit requests for replies using the actual outbox and delivery receipts."""
from collections import Counter


def requested_replies(sim, through=None, actor=None, stage="all", offset=0, limit=24):
    with sim._lock:
        end = sim.window if through is None else through
        if not 0 <= end <= sim.window:
            raise ValueError("请选择开局或已经结算的窗口")
        names = {a.id: a.name for a in sim._scenario.entities}
        if actor and actor not in names:
            raise ValueError("找不到所选主体")
        if stage not in {"all", "outstanding", "answered", "overdue"}:
            raise ValueError("回信筛选条件无效")
        letters = {}
        for event in sim.journal.request_mail(end, sim._discarded):
            data = event["data"]
            if actor and actor not in (data["from"], data["to"]):
                continue
            key = data.get("dispatch_id") or event["id"]
            if key not in letters:
                letters[key] = {"id": key, "sender": data["from"], "recipient": data["to"],
                    "sender_name": names[data["from"]], "recipient_name": names[data["to"]],
                    "sent": data.get("sent_window", event["window"]), "by": data["reply_by"],
                    "text": data["text"][:320], "excerpt": len(data["text"]) > 320,
                    "receipt": None, "arrival": None, "reply_sent": None, "reply_arrival": None, "reply_id": None}
            if event["kind"] == "message_delivered":
                letters[key].update(receipt=event["id"], arrival=event["window"])
        counts = Counter()
        selected = []
        for item in letters.values():
            responses = sim.journal.request_mail(end, sim._discarded, responses_to=item["receipt"]) if item["receipt"] else []
            if responses:
                first = responses[0]
                item.update(reply_sent=first["data"].get("sent_window", first["window"]),reply_id=first["id"])
            arrived = next((ev for ev in responses if ev["kind"] == "message_delivered"), None)
            if arrived:
                item.update(reply_arrival=arrived["window"],reply_id=arrived["id"],
                            reply_sent=arrived["data"].get("sent_window",arrived["window"]))
            postmark = arrived["data"].get("dispatch_id") if arrived else responses[0]["id"] if responses else None
            if postmark:
                sent=sim.journal.event(postmark)
                item.update(reply_dispatch=postmark,reply_due=sim.mail_timing.due(sent,end))
            if arrived:
                status = "answered"
            elif responses:
                status = "returning"
            elif item["receipt"]:
                status = "waiting"
            else:
                status = "outbound"
            due = item["by"]
            if due == "open":
                timing = "open"
            elif arrived:
                timing = "on_time" if arrived["window"] <= due else "late"
            else:
                timing = "overdue" if end > due else "due" if end == due else "upcoming"
            item.update(status=status,timing=timing)
            counts["all"] += 1
            counts["answered" if arrived else "outstanding"] += 1
            if timing == "overdue":
                counts["overdue"] += 1
            if (stage == "all" or stage == "answered" and arrived or stage == "outstanding" and not arrived
                    or stage == "overdue" and timing == "overdue"):
                selected.append(item)
        selected.sort(key=lambda m: (m["status"]=="answered", m["timing"]!="overdue",
                                      m["by"] if type(m["by"]) is int else float("inf"), m["sent"], m["id"]))
        return {"through": end, "counts": {k:counts[k] for k in ("all","outstanding","answered","overdue")},
                "actors":[{"id":key,"name":name} for key,name in names.items()], "total":len(selected),
                "items":selected[offset:offset+limit], "next":min(offset+limit,len(selected))}
