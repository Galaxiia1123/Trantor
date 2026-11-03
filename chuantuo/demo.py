"""Explicit offline scripted demonstration. Never a fallback from a real model."""
from __future__ import annotations

import json
from decimal import Decimal, ROUND_HALF_UP
from importlib.resources import files


def demo_scenario():
    from .schema import Scenario
    public = {"public": True}
    return Scenario.model_validate({
        "title": "跨层协作：港口改造协商",
        "background": "虚构海岚城正在协商港口改造，市府、港务署、一线工程师、社区和外部企业参与。此为离线脚本演示，只验证系统流程。",
        "domains": ["基础设施", "社区协作"],
        "entities": [
            {"id": "city", "name": "海岚市府", "identity": "城市治理主体，拥有独立决策权", "preferences": "权衡公共服务与施工影响",
             "fields": [{"id": "readiness", "domain": "基础设施", "kind": "number", "value": 20, "unit": "演示指数", "visibility": public}]},
            {"id": "port", "name": "港务署", "subsystem_id": "city_services", "identity": "港口组织管理者", "preferences": "保障运营",
             "materials": [{"domain": "基础设施", "text": "PRIVATE_PORT：内部备用设备信息，仅港务署可见。"}],
             "fields": [{"id": "status", "domain": "基础设施", "kind": "string", "value": "方案准备"}]},
            {"id": "engineer", "name": "一线工程师", "subsystem_id": "port_team", "identity": "掌握施工细节的个人", "preferences": "提前反映现场问题",
             "fields": [{"id": "capacity", "domain": "基础设施", "kind": "number", "value": 3, "unit": "演示工作包"}]},
            {"id": "community", "name": "沿港社区", "subsystem_id": "city_community", "identity": "居民组织", "preferences": "保护通行和生活便利",
             "fields": [{"id": "access", "domain": "社区协作", "kind": "string", "value": "正常", "visibility": public}]},
            {"id": "company", "name": "远航工程公司", "identity": "外部企业，不展开内部部门", "preferences": "保持交付质量",
             "fields": [{"id": "schedule", "domain": "基础设施", "kind": "string", "value": "等待信息", "visibility": public}]},
        ],
        "subsystems": [
            {"id": "city_services", "name": "公共服务系统", "owner_id": "city"},
            {"id": "city_community", "name": "社区协商系统", "owner_id": "city"},
            {"id": "port_team", "name": "港口现场团队", "owner_id": "port"},
        ],
        "influences": [
            {"source": "engineer", "target": "city", "weight": 0.8},
            {"source": "engineer", "target": "company", "weight": 0.65},
            {"source": "engineer", "target": "community", "weight": 0.6},
            {"source": "city", "target": "engineer", "weight": 0.4},
            {"source": "port", "target": "city", "weight": 0.7},
        ],
        "rules": [{"text": "表达是意图或沟通，不因声称完成就确认施工完成；各组织保持独立决策。"}],
        "assumptions": ["所有人物、状态和权重均为用户可编辑的虚构演示设定。"],
    })


def demo_config():
    from .schema import RunConfig
    return RunConfig.model_validate({"window_duration": "1个模拟日（演示值）", "windows": 3, "rounds_per_window": 2,
        "max_parallel": 4, "models": {"configs": {"offline": {"provider": "demo", "model": "scripted-demo", "context_limit_chars": 1000000}},
        "world": "offline", "parser": "offline", "report": "offline", "layers": {0: "offline", 1: "offline", 2: "offline"}}})


EXAMPLES = {"harbor":"港口改造协商", "release":"辅助阅读插件发布"}


def example_scenario(name):
    from .schema import Scenario
    if name == "harbor":
        return demo_scenario()
    if name == "release":
        return Scenario.model_validate_json(files("chuantuo").joinpath("templates/release.json").read_text(encoding="utf-8"))
    raise FileNotFoundError("示例不存在")


def respond(messages: list[dict[str, str]]) -> str:
    payload = json.loads(messages[-1]["content"])
    task = payload["task"]
    if task == "agent":
        actor = payload["actor_id"]
        window, round_index = payload["window"], payload["round"]
        incoming = payload["observation"]["messages"]
        view = payload["observation"]
        roster = {a["id"] for a in view["roster"]}
        port_example = {"city","engineer","port","company","community"} <= roster
        if not port_example:
            text = f"{view['self']['name']}在窗口{window}第{round_index}轮核对本方可见资料。"
            peers = [e["target"] for e in view["influences"] if e["source"] == actor and e["target"] != actor and e["weight"] > 0]
            outgoing = [{"recipients":peers[:2],"text":"请补充与你方有关的情况，我会据此评估下一轮安排。"}] if peers else []
            if incoming:
                text += f"已收到 {len(incoming)} 条直接消息，保留本主体的独立判断。"
        elif actor == "engineer":
            text = f"窗口{window}第{round_index}轮：我建议先协调现场通行，再安排勘测；这是提案，还没有施工结果。"
            outgoing = [{"recipients": ["city", "company", "community"], "text": text}]
        elif actor == "city":
            text = "我会独立评估通行与施工准备，不能只凭下属的提案认定工程完成。" + ("我已直接收到一线工程师的意见。" if incoming else "目前先检查已有公开信息。")
            outgoing = [{"recipients": ["engineer"], "text": "收到建议后我会独立评估，请补充现场观察；这不构成批准。"}]
        else:
            text = f"我作为{payload['observation']['self']['name']}继续主动评估本方条件。" + ("已收到直接沟通，我会形成自己的回应。" if incoming else "即使暂未收到消息，我仍安排准备工作。")
            outgoing = []
        for message in outgoing:
            message["recipients"] = [recipient for recipient in message["recipients"] if recipient in roster]
        result = {"text": text, "messages": [m for m in outgoing if m["recipients"]]}
    elif task == "world":
        actions = payload["actions"]
        engineer_ids = [a["id"] for a in actions if a["actor_id"] == "engineer"
                        and any("city" in msg["recipients"] for msg in a.get("messages",[]))]
        port_example = {"city","engineer","port","company","community"} <= payload["state"].keys()
        edge = next((e for e in payload["scenario"]["influences"] if e["source"]=="engineer" and e["target"]=="city"),None)
        delta = ((Decimal("6.25") * Decimal(str(edge["weight"]))).quantize(Decimal(".01"),rounding=ROUND_HALF_UP)
                 if port_example and edge and engineer_ids else Decimal(0))
        reviews = []
        for actor_id, fields in payload["state"].items():
            for field_id, value in fields.items():
                changed = actor_id == "city" and field_id == "readiness" and type(value) in (float, int) and delta != 0
                after = value
                if changed:
                    amount = Decimal(str(value)) + delta
                    after = int(amount) if type(value) is int and amount == amount.to_integral_value() else float(amount)
                reviews.append({"entity_id": actor_id, "field_id": field_id, "before": value,
                    "after": after,
                    "reason": f"港口样例规则按一线直接沟通的影响强度 {edge['weight']} × 6.25 计一次，准备指数增加 {delta:g}。" if changed else "本窗口无可确认的客观变化，保持原值。",
                    "action_ids": engineer_ids if changed else [],
                    "influence_refs":[{"source":"engineer","target":"city","action_ids":engineer_ids,
                        "explanation":"本窗口已向市府直接投递现场意见，按样例规则一次结算。"}] if changed else []})
        consequence = ({"text":"跨层信息完成交流，施工是否实际完成尚无依据。演示指数仅展示状态提交过程。", "action_ids":engineer_ids}
                       if port_example else {"text":"离线调度已记录本窗口的表达与定向投递。此脚本没有为该场景设定额外的客观变化。", "action_ids":[a["id"] for a in actions[:2]]})
        suggestions = []
        if payload["window"] == 1 and {"council","maintainers","tester","readers"} <= payload["state"].keys() and "blocked_steps" not in payload["state"]["tester"]:
            suggestions.append("可以让测试员记录键盘操作中受阻的步骤数，由用户确认字段定义、初值和可见范围。")
        result = {"reviews": reviews, "consequences":[consequence], "suggested_fields":suggestions}
    elif task == "parse":
        result = demo_scenario().model_dump(mode="json")
        result["title"] = "离线解析示例（非真实语义解析）"
        result["assumptions"].append("离线解析返回固定样例，未依据输入推断；连接真实模型后才能验证解析能力。")
    elif task == "estimate_weights":
        result = {"estimates": [{"source": source, "target": target, "weight": 0.5,
                   "reason": "离线脚本演示估计值，非真实模型判断，需用户审阅。"} for source, target in payload["missing"]]}
    elif task == "select_quotes":
        result = {"quotes": [{"source_id":item["source_id"], "page":item["page"], "quote":item["text"][:100]}
                             for item in payload["items"][:12] if item["text"]]}
    elif task == "define":
        scenario = payload.get("current_scenario") or demo_scenario().model_dump(mode="json")
        result = {"summary": "离线流程演示：以下固定港口场景仅用于体验整理、审阅与采纳，不是对上传材料的真实 AI 理解。",
                  "scenario": scenario, "changes": ["演示返回固定场景；真实模型会按目标、材料与回答生成或修订。"],
                  "questions": [{"id": "scope", "question": "你希望重点观察什么变化？", "reason": "演示问题；真实整理会针对材料提出关键缺口。", "options": ["协作与沟通", "方案形成过程"]}]}
    elif task=="field_reading":
        ref=next(e["id"] for e in payload["evidence"] if e["kind"] in {"expression","decision_deferred"})
        result={"findings":[{"index":i,"status":"unknown","reason":"本机脚本展示记录与引用，不对自定义口径作语义判断。","evidence_ids":[ref]}
                            for i in range(1,len(payload["criteria"])+1)]}
    elif task == "analysis":
        records = payload["evidence"]
        actor = payload.get("actor")
        sample = next((e for e in reversed(records) if e["kind"] == ("expression" if actor else "window_committed")), records[-1])
        result = {"text": f"离线证据示例。窗口 {sample['window']} 留有可核对的记录，节选如下。\n{sample['excerpt'][:330]}",
                  "evidence_ids": [sample["id"]], "limitations": "此回答用于演示查证与访谈流程，未进行语义推理。"}
    elif task == "report_digest":
        items = payload["items"]
        ids = list(dict.fromkeys(eid for item in items for eid in item["evidence_ids"]))
        result = {"findings": [{"text": f"离线分批示例，本批包含 {len(items)} 条记录或片段，须核对引用原文。",
                                "evidence_ids": list(dict.fromkeys([ids[0], ids[-1]]))}]}
    elif task == "report_synthesis":
        findings = payload["findings"]
        result = {"title": payload["context"]["title"] + "：离线分批验证报告", "sections": [
            {"title": "方法与覆盖", "text": f"本次离线脚本对 {payload['context']['evidence_count']} 条记录进行了分批归纳。",
             "evidence_ids": findings[0]["evidence_ids"]},
            {"title": "讨论与局限", "text": "报告解释：示例用于核验分批流程和原文引用。归纳细节及模型判断须人工核查。",
             "evidence_ids": findings[-1]["evidence_ids"]}]}
    elif task == "report":
        events = payload["events"]
        created = next(e for e in events if e["kind"] == "run_created")
        scenario = created["data"]["scenario"]
        commits = [e for e in events if e["kind"] == "window_committed"]
        expressions = [e for e in events if e["kind"] == "expression" and e["window"] <= payload["status"]["window"]]
        roles = payload.get("execution", {}).get("actor_world_configs", {})
        execution = "、".join(sorted({m["provider"]+" / "+m["model"] for m in roles.values()})) or "见初始配置"
        last = commits[-1] if commits else created
        result = {"title": f"{scenario['title']}：离线流程验证报告", "sections": [
            {"title": "场景、初始条件与假设", "text": "场景：" + scenario["title"] + "。显式主体：" + "、".join(a["name"] for a in scenario["entities"]) + "。配置的来源与假设见初始记录；此处使用离线脚本演示。", "evidence_ids": [created["id"]]},
            {"title": "仿真方法", "text": "实际使用 Concordia EntityAgent，主体各有独立记忆；全局统一窗口内按等量轮次表达，轮末交付消息，窗口末统一校验和结算。本报告由离线脚本整理。主体与世界模型配置为 " + execution + "。", "evidence_ids": [created["id"]]},
            {"title": "关键事件与状态演化", "text": f"已提交 {len(commits)} 个窗口；记录 {len(expressions)} 次主体表达。具体互动和逐字段变化请核对证据中的原文与窗口结算。", "evidence_ids": [last["id"], *(e["id"] for e in expressions[:2])]},
            {"title": "结果", "text": f"运行状态为 {payload['status']['status']}。只有校验通过的窗口被提交。有关变化须结合执行方式和情境解释。", "evidence_ids": [last["id"]]},
            {"title": "讨论与局限", "text": "报告解释：结果证明基础数据流可以运行，不能据此证明模型推理质量或现实预测效度。真实模型调用、规模性能、权重合成算法和长期记忆压缩需要后续评估。", "evidence_ids": [created["id"], last["id"]]},
        ]}
        if commits:
            result["sections"][2]["claims"] = [{"basis":"simulation",
                "text":f"引擎已提交 {len(commits)} 个统一窗口。", "evidence_ids":[last["id"]]}]
        result["sections"][-1]["claims"] = [{"basis":"interpretation","text":"对外部情境作推断还需要独立验证。",
                                               "evidence_ids":[created["id"],last["id"]]}]
    else:
        raise ValueError(f"离线演示不支持此任务: {task}")
    return json.dumps(result, ensure_ascii=False)
