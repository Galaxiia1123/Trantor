"""Exercise native text relations, one model and private inputs on a synthetic many-actor world."""
import argparse
import json
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from chuantuo.engine import Simulation, encode
from chuantuo.models import ModelConfig, ModelGateway
from chuantuo.preflight import preflight
from chuantuo.schema import RunConfig11, Scenario11
from chuantuo.settlement import verify_commits


def make_scene(count):
    units, mechanisms = [], []
    for index in range(count):
        owner = f"a{index:03}"
        peer = f"a{(index+1) % count:03}"
        units.append({"id":owner,"name":f"观测组{index:03}","identity":"独立核对观测记录并决定是否沟通。",
                      "parent_id":f"a{(index-1)//2:03}" if index else None,
                      "fields":[{"id":"reading","label":"记录状态","kind":"string","value":None,"domain":"观测协作"}],
                      "materials":[{"domain":"观测协作","text":"未公开样本编号 private-marker-"+owner,"protected_terms":["private-marker-"+owner],
                                    "sharing":{"allowed_recipients":[]}}]})
        targets = {peer}
        if index:
            targets.add("a000")
        for target in sorted(targets):
            mechanisms.append({"source":owner,"target":target,"description":"可直接递交观测摘要，由收件组独立判断是否复核。"})
    return Scenario11(title="多层观测协作",background="虚构观测网络中各组按相同时间窗口交换记录。",
                      domains=["观测协作"],entities=units,influences=mechanisms)


def benchmark(count, windows, rounds, context, parallel):
    scene = make_scene(count)
    model = ModelConfig(provider="custom",model="fixture",base_url="http://localhost",context_limit_chars=context)
    config = RunConfig11(model=model,windows=windows,rounds_per_window=rounds,max_parallel=parallel,recent_records=3)
    budget = preflight(scene,config,scene.depths())
    metrics = {"fixture":"local deterministic HTTP transport","actors":count,"levels":max(scene.depths().values())+1,
               "relations":len(scene.influences),"windows":windows,"rounds":rounds,"context_limit_chars":context,
               "expected_actor_calls":count*windows*rounds,"external_requests":0}
    if budget["blocking_context"]:
        return {**metrics,"status":"preflight_blocked","reason":budget["blocking_context"]}
    largest = {"agent":0,"world":0}
    lock = threading.Lock()
    def endpoint(request):
        messages = json.loads(request.content)["messages"]
        data = json.loads(messages[-1]["content"])
        purpose = data["task"]
        with lock:
            largest[purpose] = max(largest[purpose],sum(len(m["content"]) for m in messages))
        if purpose == "agent":
            who = data["actor_id"]
            if set(re.findall(r"private-marker-(a\d+)",messages[-1]["content"])) != {who}:
                raise AssertionError("actor prompt exceeded its private input scope")
            targets = [e["target"] for e in data["observation"]["influences"] if e["source"] == who]
            body = {"text":"本轮已核对获准查阅的记录。","messages":[{"recipients":targets,"text":"请核对本窗公开观测摘要。"}],"matters":[]}
        else:
            body = {"reviews":[{"entity_id":actor,"field_id":field,"before":value,"after":value,"reason":"无新观测值。"}
                               for actor,fields in data["state"].items() for field,value in fields.items()]}
        return httpx.Response(200,json={"model":"fixture","choices":[{"message":{"content":encode(body)}}]})
    gateway = ModelGateway(transport=httpx.MockTransport(endpoint))
    try:
        with tempfile.TemporaryDirectory(prefix="chuantuo-scale-") as folder:
            directory = Path(folder)/"run"
            start = time.perf_counter()
            sim = Simulation(scene,config,gateway,directory)
            outcome = sim.run()
            elapsed = time.perf_counter() - start
            verified = verify_commits(scene,config,sim.journal.selected(transport_details=False))
            result = {**metrics,"status":outcome["status"],"error":outcome["error"],"seconds":round(elapsed,2),
                      "committed":verified["committed_windows"],"expressions":verified["verified_expressions"],
                      "deliveries":verified["verified_deliveries"],"largest_sent_chars":largest,
                      "journal_bytes":(directory/"events.jsonl").stat().st_size}
            if outcome["status"] == "completed":
                assert verified["verified_expressions"] == metrics["expected_actor_calls"] and outcome["state"] == scene.initial_state()
                assert Simulation.restore(directory,gateway).state == outcome["state"]
            return result
    finally:
        gateway.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--actors",type=int,default=48)
    parser.add_argument("--windows",type=int,default=4)
    parser.add_argument("--rounds",type=int,default=2)
    parser.add_argument("--context",type=int,default=250000)
    parser.add_argument("--parallel",type=int,default=8)
    args=parser.parse_args()
    if not (2 <= args.actors <= 256 and 1 <= args.windows <= 100 and 1 <= args.rounds <= 10):
        parser.error("actors 2–256, windows 1–100, rounds 1–10")
    output=benchmark(args.actors,args.windows,args.rounds,args.context,args.parallel)
    print(json.dumps(output,ensure_ascii=False))
    raise SystemExit(output["status"] != "completed")
