"""Reproducible local scheduling and replay measurement with no provider credentials."""
import argparse
import json
import platform
import tempfile
import time
from contextlib import nullcontext
from pathlib import Path

from chuantuo.engine import Simulation
from chuantuo.insights import timeline
from chuantuo.schema import RunConfig, Scenario
from chuantuo.services import generate_report


def scenario_for(count):
    entities, subsystems, edges = [], [], {}
    for i in range(count):
        actor = f"a{i:03}"
        parent = (i-1)//2 if i else None
        entities.append({"id":actor, "name":f"协作单元 {i}", "identity":"独立研究团队，按可见信息决定沟通。",
            "subsystem_id":f"team{parent}" if i else None, "materials":[{"domain":"观测", "text":f"private-marker-{actor}"}],
            "fields":[{"id":"phase", "domain":"观测", "kind":"string", "value":None if i%3 else "准备中",
                       "visibility":{"public":i%7==0}}]})
        if i*2+1 < count:
            subsystems.append({"id":f"team{i}", "name":f"协作组 {i}", "owner_id":actor})
        for target, weight in [(0,.73),((i+1)%count,.35)]:
            if i != target:
                dest = f"a{target:03}"
                edges[actor,dest] = {"source":actor, "target":dest, "weight":weight}
    return Scenario.model_validate({"title":"跨组观测网络 · 离线负载样例", "background":"虚构的多层协作组织，测试等量轮次和定向消息。",
        "domains":["观测"],"entities":entities,"subsystems":subsystems,"influences":list(edges.values())})


def run(directory, count, windows, rounds, parallel, roster, actor_context):
    scene = scenario_for(count)
    levels = set(scene.depths().values())
    config = RunConfig.model_validate({"windows":windows, "rounds_per_window":rounds,"max_parallel":parallel,
        "window_duration":"一个观测周期","memory_policy":"budgeted", "world_source_policy":"defined","roster_policy":roster,
        "models":{"configs": {"actors":{"provider":"demo","model":"scripted-demo","context_limit_chars":actor_context},
                              "world":{"provider":"demo","model":"scripted-demo","context_limit_chars":4000000},
                              "report":{"provider":"demo","model":"scripted-demo","context_limit_chars":65000}},
                  "layers":{d:"actors" for d in levels},"parser":"world","world":"world","report":"report"}})
    start = time.perf_counter()
    sim = Simulation(scene,config,directory=directory)
    snap = sim.run()
    elapsed = time.perf_counter()-start
    if snap["status"] != "completed":
        return {"fixture":"synthetic_offline","status":snap["status"],"error":snap["error"],"run_id":sim.run_id,
                "actors":count,"roster_policy":roster,"actor_context_chars":actor_context,"committed_windows":snap["window"],
                "model_calls":snap["model_calls"],"provider_http_attempts":0,"run_seconds":round(elapsed,3)}
    start = time.perf_counter()
    report = generate_report(sim)
    report_time = time.perf_counter()-start
    start = time.perf_counter()
    restored = Simulation.restore(directory)
    replay_time = time.perf_counter()-start
    view = timeline(restored)
    expected = count * rounds * windows
    if view["statistics"]["expressions"] != expected or snap["state"] != restored.state:
        raise AssertionError("结构验收未通过")
    return {"fixture":"synthetic_offline","status":"completed","actor_context_chars":actor_context,"python":platform.python_version(), "agent_runtime":"trantor-local-v1",
        "os":platform.system(), "actors":count, "levels":len(levels),"edges":len(scene.influences),
        "windows":windows,"rounds_per_window":rounds,"max_parallel":parallel,"roster_policy":roster,"run_id":sim.run_id,
        **view["statistics"], "actor_world_calls":snap["model_calls"], "provider_http_attempts":0,
        "run_seconds":round(elapsed,3),"expressions_per_second":round(expected/elapsed,2),
        "report_seconds":round(report_time,3),"verified_reload_seconds":round(replay_time,3),
        "report_mode":report["coverage"]["method"],"report_batches":report["coverage"].get("batches",0),
        "journal_bytes":(directory/"events.jsonl").stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--actors",type=int,default=32)
    parser.add_argument("--windows",type=int,default=4)
    parser.add_argument("--rounds",type=int,default=2)
    parser.add_argument("--parallel",type=int,default=4)
    parser.add_argument("--roster",choices=("all","neighbors"),default="all")
    parser.add_argument("--actor-context-chars",type=int,default=24000)
    parser.add_argument("--metrics",type=Path,help="在新文件单独保留测量数据")
    parser.add_argument("--retain",type=Path,help="把日志与测量结果保存在一个新目录")
    args=parser.parse_args()
    if not (2 <= args.actors <= 256 and 1 <= args.windows <= 100 and 1 <= args.rounds <= 20 and 1 <= args.parallel <= 64):
        parser.error("actors 2–256, windows 1–100, rounds 1–20, parallel 1–64")
    if args.retain:
        args.retain.mkdir(parents=True,exist_ok=False)
    with (nullcontext(args.retain) if args.retain else tempfile.TemporaryDirectory(prefix="chuantuo-benchmark-")) as work:
        result=run(Path(work),args.actors,args.windows,args.rounds,args.parallel,args.roster,args.actor_context_chars)
        output=json.dumps(result,ensure_ascii=False,indent=2)
        if args.retain:
            (args.retain/"benchmark.json").write_text(output,encoding="utf-8")
        if args.metrics:
            with args.metrics.open("x",encoding="utf-8") as stream:
                stream.write(output)
        print(output)
        return int(result["status"] != "completed")


if __name__ == "__main__":
    raise SystemExit(main())
