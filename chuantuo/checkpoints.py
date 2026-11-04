"""Operator-selected, one-shot pauses evaluated only at committed window boundaries."""
import re

from .observatory import FieldWatch


KINDS = {"pause_goal_set", "pause_goal_reached", "pause_goal_cancelled", "pause_goal_expired"}
STATUSES = {"pause_goal_reached":"reached", "pause_goal_cancelled":"cancelled", "pause_goal_expired":"expired"}


def pause_goals(sim):
    goals = {}
    for event in sim.journal.selected(KINDS, transport_details=False):
        data = event["data"]
        if data["run_id"] != sim.run_id:
            continue
        key = data["goal_id"]
        if event["kind"] == "pause_goal_set":
            goals[key] = {"id":key, "question":data["question"], "armed_after":data["armed_after"],
                          "status":"watching", "record":event["id"], "window":event["window"]}
        elif key in goals:
            goals[key].update(status=STATUSES[event["kind"]], record=event["id"], window=event["window"])
            if data.get("commit_id"):
                goals[key]["commit_id"] = data["commit_id"]
    if sim.status in {"completed","stopped"}:
        for goal in goals.values():
            if goal["status"] == "watching":
                goal["status"] = "ended"
    return list(goals.values())


def arm_goal(sim, goal_id, question):
    if not re.fullmatch(r"[a-f0-9]{32}", goal_id):
        raise ValueError("暂停条件编号无效")
    with sim._lock:
        actor, field, target = question.resolve(sim._scenario, sim.config.catalog.values())
        goals = pause_goals(sim)
        existing = next((g for g in goals if g["id"] == goal_id), None)
        spec = question.model_dump(mode="json")
        if existing:
            if existing["question"] != spec:
                raise ValueError("该编号已经保存了另一项暂停条件")
            return existing
        if sim.status in {"completed","stopped"}:
            raise ValueError("这条路线已经结束，可在新路线中设置暂停条件")
        if question.operator == "track" or question.criterion != "ever":
            raise ValueError("请为暂停条件指定一个目标，选择曾经达到的观察方式")
        if question.deadline is not None and question.deadline <= sim.window:
            raise ValueError("暂停条件的期限需要晚于当前已结算窗口")
        if question.matches(sim._state[actor.id][field.id], target) is True:
            raise ValueError("这个条件在当前状态已经满足，可直接暂停并查看结果")
        if sum(g["status"] == "watching" for g in goals) >= 20:
            raise ValueError("一条路线最多同时关注 20 项暂停条件")
        event = sim.journal.append("pause_goal_set", {"run_id":sim.run_id, "goal_id":goal_id,
                                   "question":spec, "armed_after":sim.window}, sim.window)
        return {"id":goal_id, "question":spec, "armed_after":sim.window, "status":"watching",
                "record":event["id"], "window":event["window"]}


def cancel_goal(sim, goal_id):
    with sim._lock:
        goal = next((g for g in pause_goals(sim) if g["id"] == goal_id), None)
        if goal is None:
            raise FileNotFoundError("这条路线没有这个暂停条件")
        if goal["status"] == "watching":
            sim.journal.append("pause_goal_cancelled", {"run_id":sim.run_id, "goal_id":goal_id}, sim.window)
        return {"id":goal_id}


def evaluate_goals(sim):
    with sim._lock:
        reached = False
        for goal in pause_goals(sim):
            if goal["status"] != "watching" or sim.window <= goal["armed_after"]:
                continue
            question = FieldWatch.model_validate(goal["question"])
            actor, field, target = question.resolve(sim._scenario, sim.config.catalog.values())
            value = sim._state[actor.id][field.id]
            within = question.deadline is None or sim.window <= question.deadline
            if within and question.matches(value, target) is True:
                kind = "pause_goal_reached"
                reached = True
            elif question.deadline is not None and sim.window >= question.deadline:
                kind = "pause_goal_expired"
            else:
                continue
            commit = sim.journal.settlement(sim.window)
            sim.journal.append(kind, {"run_id":sim.run_id, "goal_id":goal["id"], "value":value,
                                      "commit_id":commit["id"]}, sim.window)
        if reached:
            sim._pause.set()
        return reached
