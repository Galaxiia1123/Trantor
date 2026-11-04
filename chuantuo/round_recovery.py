"""Resume an unchanged, fully observed round without repeating its successful model calls."""
from .schema import Decision, RunConfig11, validate_outbox
from .settlement import verify_commits


def resume_round(sim, events):
    if not isinstance(sim.config,RunConfig11):
        return False
    edge=max((i for i,e in enumerate(events) if e["kind"] in {"window_committed","run_recovered"}),default=0)
    tail=[e for e in events[edge+1:] if e["id"] not in sim._discarded]
    pending=next((e for e in reversed(tail) if e["kind"]=="round_pending"),None)
    if pending is None:
        return False
    turn=pending["data"]["round"]
    observations=[e for e in tail if e["kind"]=="observation"]
    if not observations or max(e["data"]["visible"]["round"] for e in observations)!=turn:
        return False
    if any(e["kind"] in {"expression","decision_deferred"} and e["data"]["round"]==turn for e in tail):
        return False
    seen={e["data"]["actor_id"]:e for e in observations if e["data"]["visible"]["round"]==turn}
    actors=set(sim.agents)
    accepted=pending["data"]["outputs"]
    failed=set(pending["data"]["failed"])
    waiting=set(pending["data"]["waiting"])
    if (set(seen)!=actors or set(accepted)|failed|waiting!=actors
            or set(accepted)&failed or set(accepted)&waiting or failed&waiting):
        raise ValueError("待恢复轮次的主体、观察或模型结果不完整")
    verify_commits(sim._scenario,sim.config,events)
    for actor,ref in accepted.items():
        response=sim.journal.event(ref)
        request=sim.journal.event(response["data"]["input_id"])
        config_id=sim.config.model_id("actor",sim.depths[actor])
        if (response["kind"]!="model_output" or request["kind"]!="model_input" or ref in sim._discarded
                or response["window"]!=pending["window"] or response["data"].get("actor_id")!=actor
                or request["data"].get("actor_id")!=actor or response["data"].get("purpose")!="agent"
                or request["window"]!=pending["window"] or request["data"].get("purpose")!="agent"
                or response["data"].get("config_id")!=config_id or request["data"].get("config_id")!=config_id
                or not int(seen[actor]["id"][1:])<int(request["id"][1:])<int(ref[1:])<int(pending["id"][1:])):
            raise ValueError("待复用的模型结果不属于该主体这次观察之后的请求")
        result=Decision.model_validate_json(response["data"]["text"])
        if any(not set(m.recipients)<=actors for m in result.messages):
            raise ValueError("待复用的模型消息包含不存在的收件人")
        validate_outbox(result,sim._perception.delivery_limits)
        sim.journal.validate_replies(result,actor,seen[actor]["id"],sim._discarded)
    sim._restore_boundary(events,frozen=True,resuming_round=turn)
    actual_waiters={actor for actor,observation in sim._observations.items()
                    if (plan:=sim._plans.get(actor)) and plan.wake_reason(observation) is None}
    if actual_waiters!=waiting:
        raise ValueError("恢复后的延续等待安排与原轮次不一致")
    sim._resume_round=turn
    sim._resume_outputs=dict(accepted)
    sim._frozen_window=None
    # Documents must be checked after reconstructing the earlier rounds' deliveries.
    for actor,ref in accepted.items():
        result=Decision.model_validate_json(sim.journal.event(ref)["data"]["text"])
        sim._perception.documents.validate(result,seen[actor]["data"]["visible"])
    sim.journal.append("round_resume_requested",{"round":turn,"frozen_observations":{a:e["id"] for a,e in seen.items()},
                       "candidate_outputs":accepted,"retry_actors":sorted(failed),"waiting":sorted(waiting),
                       "reason":"保留每个主体本轮各自的观察。仅当模型输入完全相同时复用已完成结果，全部主体就绪后统一记录行动和投递。"},pending["window"])
    return True


def reusable_output(sim, actor, config_id, messages):
    ref=sim._resume_outputs.pop(actor,None)
    if not ref:
        return None
    output=sim.journal.event(ref)
    request=sim.journal.event(output["data"]["input_id"])
    if request["data"].get("config_id")!=config_id or request["data"]["messages"]!=messages:
        return None
    return output
