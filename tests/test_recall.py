import json

from chuantuo.continuity import actor_instruction
from chuantuo.engine import Simulation
from chuantuo.experiments import ExperimentRequest, fork_experiment
from chuantuo.schema import RunConfig11, Scenario11


class RecallGateway:
    def __init__(self):
        self.requests=[]

    def complete(self, config, messages):
        payload=json.loads(messages[-1]["content"])
        self.requests.append(payload)
        if payload["task"]=="world":
            return '{"reviews":[],"consequences":[]}'
        actor,window=payload["actor_id"],payload["window"]
        if actor=="a" and window==1:
            return json.dumps({"text":"自己的备用安排是蓝绳七日。",
                               "messages":[{"recipients":["b"],"text":"旧条款的纸函归还期限为第十窗。"}]},ensure_ascii=False)
        if actor=="b" and window==6:
            return json.dumps({"text":"准备核对此前约定。","recall_queries":["甲的旧条款归还期限","对方的备用安排"]},ensure_ascii=False)
        return '{"text":"继续处理当前事项。","messages":[]}'


def test_actor_requested_recall_uses_own_received_copy_after_restore_and_fork(tmp_path):
    scene=Scenario11(title="回忆条款",background="两个独立主体。",domains=["往来"],
                     entities=[{"id":"a","name":"甲","identity":"独立主体"},{"id":"b","name":"乙","identity":"独立主体"}])
    config=RunConfig11.model_validate({"model":{"provider":"demo","model":"recall-fixture"},
                                      "windows":6,"rounds_per_window":1,"recent_records":1,"retrieval_count":1})
    gateway=RecallGateway()
    original=Simulation(scene,config,gateway,tmp_path/"base")
    assert original.run()["status"]=="completed" and original.snapshot()["model_calls"]==18
    before=(original.journal.directory/"events.jsonl").read_bytes()
    restored=Simulation.restore(original.journal.directory,gateway)
    assert restored.notebooks["b"].recall_queries==["甲的旧条款归还期限","对方的备用安排"]
    gateway.requests.clear()
    child=fork_experiment(restored,ExperimentRequest(kind="extend",window=6,through=7,extension_limit=3),
                          "a"*12,None,tmp_path/"continued")
    assert child.run()["status"]=="completed" and len(gateway.requests)==3
    recipient=next(p for p in gateway.requests if p["task"]=="agent" and p["actor_id"]=="b")
    memory=recipient["memory"]
    assert memory["retrieval_queries"]==restored.notebooks["b"].recall_queries
    assert len(memory["retrieved"])==1 and memory["retrieved"][0]["text"]=="旧条款的纸函归还期限为第十窗。"
    receipt=child.journal.event(memory["retrieved"][0]["receipt_id"])
    assert receipt["kind"]=="message_delivered" and receipt["data"]["to"]=="b"
    assert memory["retrieved"][0]["from_name"]=="甲"
    assert "蓝绳七日" not in json.dumps(recipient,ensure_ascii=False)
    sender=next(p for p in gateway.requests if p["task"]=="agent" and p["actor_id"]=="a")
    assert "retrieval_queries" not in sender["memory"]
    context=child.journal.decision_context("b",7,1,child._discarded)
    assert context["memory"]==memory and not child.notebooks["b"].recall_queries
    assert (original.journal.directory/"events.jsonl").read_bytes()==before
    assert "recall_queries" not in actor_instruction(config.model_copy(update={"retrieval_count":0}))
