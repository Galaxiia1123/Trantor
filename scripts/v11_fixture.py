"""Deterministic acceptance counterparty and publisher of the clearly labelled, read-only case."""
import hashlib
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from chuantuo.engine import Simulation, encode
from chuantuo.insights import watch_window
from chuantuo.models import ModelConfig
from chuantuo.preparation import Coverage, Seed, prepare
from chuantuo.schema import Source, load_config, load_scenario
from chuantuo.services import generate_report
from chuantuo.settlement import verify_commits

CONDITION = "潮汐-17：海岬航运承诺，若第8窗前通过护岸复核，为扩建提供2000万元书面担保，否则失效。"
NAMES = {"council":"市议会","port":"港务署","engineer":"许澄","carrier":"海岬航运","resident":"林遥"}


def sample_source():
    return Source(id="tidal_brief", name="潮汐港扩建听证.md", text=(ROOT/"chuantuo/templates/tidal-port.md").read_text(encoding="utf-8"))


def definition(data):
    src = next(s for s in data["sources"] if "潮汐港" in s["text"])
    def origin(quote):
        assert quote in src["text"]
        return {"kind":"material", "source_ids":[src["id"]], "quotes":[{"source_id":src["id"], "quote":quote}], "explanation":"用户文稿"}
    ids = list(NAMES)
    identities = ["审批扩建议案", "维护港区航道", "复核工程条件", "决定是否提供担保", "代表附近居民"]
    preferences = ["重视可核验的资金来源", "维持航道开放", "坚持工程标准", "优先履行有条件的书面约定", "希望保留夜间安宁"]
    rows = [{"id":k,"name":NAMES[k],"identity":identities[i],"preferences":preferences[i],"provenance":origin(NAMES[k])} for i,k in enumerate(ids)]
    council, port, engineer, carrier, resident = rows
    port["subsystem_id"] = "public_service"
    engineer["parent_id"] = "port"
    def field(key, label, value, quote, public, unit=None, readers=()):
        return {"id":key,"label":label,"domain":"扩建听证","value":value,"unit":unit,
                "kind":"number" if isinstance(value,int) else "string","visibility":{"public":public,"readers":list(readers)},"provenance":origin(quote)}
    council["fields"] = [field("gap","资金缺口",2000,"扩建资金缺口为 2000 万元",True,"万元"),
                         field("agenda","议案","等待复核和书面担保","才会再次审议",True)]
    port["fields"] = [field("review","护岸复核","待复核","护岸复核",False,readers=["engineer"])]
    carrier["fields"] = [field("ceiling","担保上限",2000,"担保上限为 2000 万元",False,"万元")]
    carrier["materials"] = [{"text":CONDITION,"domain":"扩建听证","provenance":origin("否则到期失效")}]
    def edge(source,target,description,quote):
        return {"source":source, "target":target, "description":description, "provenance":origin(quote)}
    scene = {"format_version":"1.1","title":"潮汐港的窗口期","background":"潮汐港正在审议扩建，各方将决定如何回应资金和工程约束。",
             "domains":["扩建听证"],"entities":rows,"subsystems":[{"id":"public_service","name":"公共服务","owner_id":"council"}],
             "influences":[edge("engineer","council","可直接递交工程复核意见，不能替议会批准。","许澄可以将复核意见直接交给议会"),
                edge("carrier","engineer","可提供有期限的担保意向，由工程师决定是否转达。","海岬航运可向许澄发送附条件的担保意向"),
                edge("resident","port","可要求公开施工时段。","居民代表可以要求公开施工时段")],
             "rules":[{"text":"未见书面担保与工程复核，议会不再次审议。担保意向不等于支付。","visibility":{"public":True},
                       "provenance":origin("只有取得有效书面担保并完成工程复核才会再次审议")}], "sources":[]}
    return {"summary":"五方围绕一项港口扩建议案互动。观察书面担保能否在工程复核后生效，以及市议会何时重启审议。",
            "scenario":scene,"plan":{"windows":3,"rounds_per_window":1,"recent_records":3,"history_windows":2},
            "observation_questions":[{"actor_id":"council","field_id":"agenda","title":"议案何时进入复议？","operator":"equals","target":"已列入复议"}],
            "plan_reason":"按文稿约定逐日推演，每窗留出回应机会。保留近期沟通和未完成事项。",
            "checklist":dict.fromkeys(Coverage.model_fields, "grounded"),"gaps":[],"findings":[]}


def decision(data):
    who, window, turn = data["actor_id"], data["window"], data["round"]
    view, notebook = data["observation"], data["memory"]
    received = [m["text"] for m in view["messages"]]
    messages, matters = [], []
    text = {"council":"书面担保与工程复核仍分别核对。","port":"按现有时段维持航道开放。",
            "engineer":"记录现场条件，核查能否提交复核。","carrier":"核对既有约定的履行条件。","resident":"核对向居民公开的施工时段。"}[who]
    def send(to, content):
        messages.append({"recipients":[to],"text":content})
    def track(value, status="open"):
        matters.append({"key":"guarantee","text":value,"status":status})
    if who == "carrier" and window == 1 and turn == 1:
        text = "向工程师发出附条件的私下担保意向。"
        promise = next(m["text"] for m in view["visible_materials"] if m["owner_id"] == "carrier")
        send("engineer",promise); track(promise)
    if who == "engineer" and any("潮汐-17" in m for m in received):
        promise = next(m for m in received if "潮汐-17" in m)
        text = "接受核验任务，保留担保条件与到期日。"; track(promise)
        send("carrier","收到；取得护岸复核结果后通知你。")
    if who == "resident" and window == 3 and turn == 1:
        text = "请求港务署公开夜间施工安排。"; send("port",text)
    if who == "port" and any("夜间施工" in m for m in received):
        text = "本期保持夜间航道开放。"; send("resident",text)
    if who == "port" and window == 7 and turn == 1:
        text = "已核验现场记录，护岸复核通过。"; send("engineer",text)
    if who == "engineer" and any("复核通过" in m for m in received) and "guarantee" in notebook["open_matters"]:
        kept = notebook["open_matters"]["guarantee"]["text"]
        if "第8窗前" in kept and "2000万元" in kept and window < 8:
            text = "在承诺期限前将复核意见直接递交议会，并请承运商出具书面担保。"
            send("council","护岸复核通过，现直接提交工程意见；尚未收到担保款。")
            send("carrier","已于第7窗完成护岸复核，请履行2000万元书面担保。")
    if who == "carrier" and any("已于第7窗" in m for m in received) and "guarantee" in notebook["open_matters"]:
        text = "签发并送达2000万元书面担保，尚未向项目付款。"
        send("council",text); send("engineer",text); track(text,"closed")
    if who == "engineer" and any("签发并送达" in m for m in received):
        text = "收到书面担保副本，结束承诺跟踪。"; track(text,"closed")
    if who == "council" and any("签发并送达" in m for m in received):
        recent = encode(notebook["recent"] + notebook["retrieved"])
        if "复核通过" in recent:
            text = "已取得直接复核意见和书面担保，决定将扩建议案列入复议；资金缺口仍由财务核对。"
    return {"text":text, "messages":messages, "matters":matters}


def settle(data):
    external={(v["entity_id"],v["field_id"]) for event in data.get("external_events",[]) for v in event["assignments"]}
    reviews, consequences = [], []
    for owner, fields in data["state"].items():
        for key, before in fields.items():
            if (owner,key) in external:
                continue
            after, reason, ids = before, "本窗缺少改变该字段的证据。", []
            def actions(actor, phrase):
                return [a["id"] for a in data["actions"] if a["actor_id"] == actor and phrase in a["text"]]
            if key == "review" and before == "待复核" and (refs := actions("port","护岸复核通过")):
                after, reason, ids = "已通过","港务署完成现场复核。", refs
            if key == "agenda" and before != "已列入复议" and (refs := actions("council","决定将扩建议案列入复议")):
                after, reason, ids = "已列入复议","议会收到现场复核与书面担保后重新审议。", refs
            if after != before:
                consequences.append({"text":reason, "action_ids":ids})
            reviews.append({"entity_id":owner,"field_id":key,"before":before,"after":after,"reason":reason,"action_ids":ids})
    return {"reviews":reviews,"consequences":consequences,"suggested_fields":[]}


def response(data):
    task = data["task"]
    if task == "prepare_scene": return definition(data)
    if task == "agent": return decision(data)
    if task == "world": return settle(data)
    if task == "analysis":
        if (data.get("actor") or {}).get("id") == "resident":
            assert "潮汐-17" not in encode(data)
        item = data["evidence"][0]
        return {"text":"这条记录是本次问题可核对的依据。","evidence_ids":[item["id"]],
                "limitations":"回答只使用本次提供的记录。"}
    if task == "report":
        events = data["events"]
        c = next(e for e in events if e["kind"] == "window_committed" and any(row["field_id"] == "agenda" for row in e["data"]["changes"]))
        first, last = events[0]["id"], c["id"]
        return {"title":"潮汐港扩建听证：有条件担保与复议","sections":[
            {"title":"场景与方法","text":"本例采用虚构港口文稿，五方在相同窗口内交流。案例回放由预设脚本生成。","evidence_ids":[first]},
            {"title":"结果","text":"护岸复核意见直接到达议会，书面担保送达后议案进入复议，2000 万元资金缺口仍然存在。",
             "evidence_ids":[last],"claims":[{"basis":"simulation","text":"议案进入复议，未发生付款。","evidence_ids":[last]}]},
            {"title":"局限","text":"该路径依赖案例设定和脚本决策，不能用于预测现实港口决策。","evidence_ids":[first,last]}]}
    raise AssertionError("unexpected fixture task " + task)


class FixtureGateway:
    def complete(self, config, messages):
        return encode(response(json.loads(messages[-1]["content"])))


def build(directory, windows=None):
    model = ModelConfig(provider="custom",model="tidal-script",base_url="http://127.0.0.1:1",context_limit_chars=2_000_000)
    gateway = FixtureGateway()
    source = sample_source()
    bundle = prepare(Seed(sources=[source], overrides={"windows":windows} if windows else {}),
                     model, gateway, None, lambda *_: None)
    sim = Simulation(load_scenario(bundle["scenario"]),load_config(bundle["config"]),gateway,directory)
    for record in bundle["preparation_records"]:
        sim.journal.append(record["kind"],record)
    result = sim.run()
    assert result["status"] == "completed", result["error"]
    assert result["state"]["council"] == {"gap":2000,"agenda":"已列入复议"}, result["state"]
    report = generate_report(sim)
    checks = verify_commits(sim.scenario,sim.config,sim.journal.selected(transport_details=False))
    return sim, report, checks


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="chuantuo-v11-case-") as folder:
        sim, report, checks = build(Path(folder)/"run")
        windows = [watch_window(sim,i) for i in range(sim.window + 1)]
        for item in windows:
            item["mode"] = "sample"
        kept = sim.journal.snapshot({"run_created","expression","window_committed","message_delivered"})
        exported = {"kind":"scripted_replay", "source_sha256":hashlib.sha256(sample_source().text.encode()).hexdigest(),
                    "windows":windows,"report":{"markdown":report["markdown"]},"evidence":{e["id"]:e for e in kept}}
        path = ROOT/"chuantuo/templates/tidal-replay.json"
        path.write_text(json.dumps(exported, ensure_ascii=False, separators=(",",":")),encoding="utf-8")
        print(encode({k:v for k,v in checks.items() if k != "state"}))
