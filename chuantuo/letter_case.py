"""A deterministic counterfactual visitors can operate without a model account."""
from pathlib import Path

from .case_runs import sample_pair
from .experiments import ExperimentRequest
from .schema import RunConfig11, Scenario11


MODEL = "letter-desk-v2"
SIGNED = "第 2 窗开始时，有效签字函已送达收件台。"
ACCEPT = "核对签章并将申请排入复核。"


def opening():
    public = {"public":True}
    return Scenario11.model_validate({
        "title":"一封签字函的两条路线","background":"渡口停航。维修社请求运营社重新审议通航安排，办理期限为三个工作日。",
        "domains":["办理进度"],
        "entities":[
            {"id":"shop","name":"维修社","identity":"提交申请和担保意向的维修机构","preferences":"希望尽快复核，不能代替运营社受理。"},
            {"id":"desk","name":"运营社","identity":"独立核对签章并决定是否排入复核的机构","preferences":"先核验签字件，再登记议程。",
             "fields":[{"id":"agenda","label":"复核安排","domain":"办理进度","kind":"string","value":"待签收",
                        "visibility":public,"constraints":{"choices":["待签收","已排入复核"]}},
                       {"id":"paid","label":"实际到账","domain":"办理进度","kind":"number","value":0,"unit":"万元",
                        "visibility":public,"constraints":{"minimum":0,"maximum":0,"fixed":True}}]}],
        "influences":[{"source":"shop","target":"desk","description":"维修社可提交材料并要求答复，受理决定由运营社作出。"},
                      {"source":"desk","target":"shop","description":"运营社告知材料要求和办理决定，维修社可据此安排提交。"}],
        "rules":[{"text":"有效签字件和运营社自己的核验决定都是登记复核的前提。担保意向不会产生到账。","visibility":public}],
        "assumptions":["虚构的三日办理过程，主体和世界的应答由本机案例脚本提供。"]})


def scripted_response(payload):
    """Only facts in the actor's own observation can activate its decision."""
    answer=_response(payload)
    if payload["task"]=="agent" and payload["window"]<3 and payload.get("latest_review_window"):
        answer["review_at"]=payload["latest_review_window"]
    return answer


def _response(payload):
    if payload["task"] == "agent":
        actor, window, view = payload["actor_id"], payload["window"], payload["observation"]
        if window == 1:
            if actor == "shop":
                return {"text":"递交复核申请并承诺补交有效签字函。","messages":[{"recipients":["desk"],"text":"我方请求复核，将补交签字函。担保意向不构成付款。"}],
                        "matters":[{"key":"letter","text":"在三个工作日内等候运营社的收件答复。"}]}
            return {"text":"告知申请方签章要求，等候签字函。","messages":[{"recipients":["shop"],"text":"请补交有效签字函，由我社核验后决定是否登记。"}]}
        if actor == "desk":
            if any(r["text"] == SIGNED for r in view["rules"]):
                if window == 2:
                    return {"text":ACCEPT,"messages":[{"recipients":["shop"],"text":"签章已核对，申请已登记复核。"}]}
                return {"text":"核对已登记的议程，等待复核会议。"}
            return {"text":"收件记录里仍没有可供核验的签字函，暂不登记。"}
        if any("已登记复核" in m["text"] for m in view["messages"]):
            return {"text":"收到登记答复，结清跟进事项并准备会议材料。","matters":[{"key":"letter","status":"closed"}]}
        return {"text":"跟进签字函的收件情况，保留申请并等待答复。"}
    if payload["task"] == "world":
        state=payload["state"]["desk"]
        decision=next((a for a in payload["actions"] if a["actor_id"]=="desk"),None)
        signed=any(r["text"]==SIGNED for r in payload["scenario"].get("rules",[]))
        accepted=signed and decision is not None and decision["text"]==ACCEPT
        action_ids=[decision["id"]] if decision else []
        after="已排入复核" if accepted else state["agenda"]
        reason=("有效函件已到，运营社核验后登记复核。" if accepted else
                "既有复核登记继续有效。" if after=="已排入复核" else
                "函件到达，但运营社尚未看到收件通知，也没有作出登记决定。" if signed else "尚无已核验的签字函，申请仍待签收。")
        return {"reviews":[{"entity_id":"desk","field_id":"agenda","before":state["agenda"],"after":after,
                            "reason":reason,"action_ids":action_ids},
                           {"entity_id":"desk","field_id":"paid","before":state["paid"],"after":state["paid"],
                            "reason":"担保意向未触发付款。","action_ids":[]}],
                "consequences":[{"text":reason,"action_ids":action_ids}]}
    return None


def run_example(root: Path, choice, gateway):
    config=RunConfig11.model_validate({"model":{"provider":"demo","model":MODEL},"windows":3,"max_review_interval":2,
                                       "window_duration":"1 个工作日","rounds_per_window":1})
    observed=choice=="notified"
    spec=ExperimentRequest(window=1,through=3,kind="repeat" if choice=="repeat" else "condition",
                           condition="" if choice=="repeat" else SIGNED,audience="readers" if observed else "world",
                           readers=["desk"] if observed else [])
    return sample_pair(root,gateway,opening(),config,spec,choice,2)
