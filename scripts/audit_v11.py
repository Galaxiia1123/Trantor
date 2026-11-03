"""Read-only audit of native run receipts and the path of explicitly confidential literals."""
import argparse
import json
import sys
from collections import Counter
from copy import deepcopy
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from chuantuo.engine import Journal
from chuantuo.schema import Scenario11, load_config, load_scenario
from chuantuo.settlement import verify_commits


def audit(directory):
    journal = Journal.open(directory)
    created = journal.event("e000001")["data"]
    scene, config = load_scenario(created["scenario"]), load_config(created["config"])
    if not isinstance(scene,Scenario11):
        raise ValueError("此检查用于 v1.1 运行")
    verified = verify_commits(scene,config,journal.selected(transport_details=False))
    public = scene.public_amounts()
    known = {}
    for actor in scene.entities:
        for m in actor.materials:
            for term in m.protected_terms:
                if term not in public:
                    known.setdefault(term,set()).update([actor.id,*m.visibility.readers])
    limits = scene.delivery_limits()
    boundary = deepcopy(known)
    unexpected, forbidden, views = [], [], Counter()
    receipts, tokens, modes = Counter(), Counter(), Counter()
    offset = 0
    while True:
        page = journal.page(offset,64)
        for event in page["events"]:
            d, kind = event["data"], event["kind"]
            if kind == "observation":
                owner = d["actor_id"]
                for term, readers in known.items():
                    if any(term in m["text"] for m in d["visible"]["messages"]):
                        readers.add(owner)
                    if any(isinstance(field["value"],str) and term in field["value"]
                           for fields in d["visible"]["visible_state"].values() for field in fields.values()):
                        readers.add(owner)
            elif kind == "model_input" and d["purpose"] == "agent":
                owner = d["actor_id"]
                views[owner] += 1
                content = d["messages"][-1]["content"]
                for term,readers in known.items():
                    if term in content and owner not in readers:
                        unexpected.append({"actor":owner,"input":event["id"]})
            elif kind == "message_delivered":
                if any(term in d["text"] and d["to"] not in audience for term,audience in limits.items()):
                    forbidden.append(event["id"])
            elif kind == "window_committed":
                boundary = deepcopy(known)
            elif kind == "run_recovered":
                known = deepcopy(boundary)
            elif kind == "provider_attempt":
                receipts[str(d.get("http_status"))] += 1
                modes[d.get("transport","unknown")] += 1
                u = d.get("usage",{})
                tokens["input"] += u.get("prompt_tokens",u.get("input_tokens",0))
                tokens["output"] += u.get("completion_tokens",u.get("output_tokens",0))
        if not page["has_more"]:
            break
        offset = page["next_offset"]
    return {"run":created["run_id"],"version":"1.1","verified":verified,
            "private_inputs_checked":dict(views),"protected_literals":len(known),"unexpected_private_inputs":unexpected,
            "forbidden_deliveries":forbidden,"transport":dict(modes),"http_status":dict(receipts),"reported_tokens":dict(tokens)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("directory",type=Path)
    result = audit(parser.parse_args().directory)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(bool(result["unexpected_private_inputs"] or result["forbidden_deliveries"]))
