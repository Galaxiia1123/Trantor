"""Compare private context encodings on one frozen transcript, without calling a model."""
import argparse
import json
import tempfile
from collections import defaultdict
from pathlib import Path

import tiktoken
from v11_fixture import CONDITION, build
from chuantuo.engine import Simulation, encode
from chuantuo.schema import RunConfig11


def compare(simulation, check_case=False):
    if not isinstance(simulation.config, RunConfig11):
        raise ValueError("请选择 v1.1 运行记录")
    tokenizer = tiktoken.get_encoding("cl100k_base")
    def tokens(text):
        return len(tokenizer.encode_ordinary(text))

    histories = defaultdict(list)
    boundary_sizes, current = {}, {}
    notebook_input = full_input = output = notes_output = calls = checks = 0
    kinds = {"model_input","model_output","expression","window_committed","run_recovered"}
    for event in simulation.journal.snapshot(kinds):
        kind, data = event["kind"], event["data"]
        if kind == "window_committed":
            boundary_sizes = {who:len(items) for who,items in histories.items()}
        elif kind == "run_recovered":
            for who, items in histories.items():
                del items[boundary_sizes.get(who,0):]
        elif kind == "model_input" and data["purpose"] == "agent":
            who = data["actor_id"]
            system, user = data["messages"]
            payload = json.loads(user["content"])
            current[who] = payload
            instruction = tokens(system["content"])
            notebook_input += instruction + tokens(user["content"])
            full_input += instruction + tokens(encode({**payload,"memory":{"history":histories[who]}}))
            calls += 1
            if check_case and who == "engineer" and payload["window"] == 7 and payload["round"] == 2:
                assert payload["memory"]["open_matters"]["guarantee"]["text"] == CONDITION
                checks += 1
            if check_case and who in {"resident","port","council"}:
                assert "潮汐-17" not in user["content"]
        elif kind == "model_output" and data["purpose"] == "agent":
            output += tokens(data["text"])
            try:
                decision = json.loads(data["text"])
            except ValueError:
                continue
            if isinstance(decision,dict) and "matters" in decision:
                # Compare equivalent representations so whitespace style is not charged to notes.
                without_notes = {key:value for key,value in decision.items() if key != "matters"}
                notes_output += tokens(encode(decision)) - tokens(encode(without_notes))
        elif kind == "expression":
            who = data["actor_id"]
            histories[who].extend(current[who]["observation"]["messages"])
            histories[who].append({"id":event["id"],"window":event["window"],**json.loads(data["raw"])})
    result = {"windows":simulation.window,"encoding":"cl100k_base / message text only","actor_calls_each":calls,
              "notebook_input_tokens":notebook_input,"full_history_input_tokens":full_input,"same_output_tokens":output,
              "estimated_matters_output_tokens":notes_output,"extra_memory_calls":0,
              "input_reduction_percent":round(100*(1-notebook_input/full_input),1) if full_input else None}
    usage = simulation.journal.usage().get("主体", {})
    if usage.get("with_usage"):
        result["provider_reported_actor_tokens"] = {k:usage[k] for k in ("input_tokens","output_tokens","with_usage","attempts")}
    if check_case:
        assert checks == 1 and simulation.state["council"]["gap"] == 2000
        result.update(due_condition_preserved=True,unauthorized_views_containing_private_code=0)
    return result


def measure(windows):
    with tempfile.TemporaryDirectory(prefix="chuantuo-memory-") as tmp:
        sim, _, _ = build(Path(tmp)/"run",windows)
        return compare(sim,check_case=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--windows",type=int,nargs="+")
    inputs.add_argument("--run",type=Path,help="对已有 v1.1 日志作只读测量")
    args = parser.parse_args()
    if args.run:
        print(json.dumps(compare(Simulation.restore(args.run)),ensure_ascii=False))
    else:
        windows = args.windows or [9,36]
        if min(windows) < 9:
            parser.error("the case requires at least nine windows")
        for count in windows:
            print(json.dumps(measure(count),ensure_ascii=False))
